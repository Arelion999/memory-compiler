"""
Hybrid search: Whoosh BM25F + sentence-transformers semantic search.
"""
import pickle
import re
from typing import Optional


# ─── Query confidence ────────────────────────────────────────────────────────
# Industry pattern (LangChain/LlamaIndex/Cohere): don't run RAG on generic queries —
# they return semantically-related noise. Detect and reject upstream.

# Russian + English stopwords + continuation/meta verbs that should never trigger
# semantic retrieval on their own.
_STOPWORDS = frozenset([
    # Russian basics
    "и", "в", "на", "с", "к", "по", "из", "за", "для", "от", "до", "о", "об",
    "у", "при", "под", "над", "что", "как", "это", "то", "тот", "та", "те",
    "так", "не", "ни", "же", "ли", "бы", "если", "или", "а", "но", "да", "нет",
    # Russian continuation / meta verbs (common false-trigger source)
    "продолжим", "продолжаем", "продолжай", "продолжить", "давай", "давайте",
    "дальше", "ещё", "еще", "помоги", "сделай", "пожалуйста", "теперь", "сейчас",
    "вот", "там", "тут", "здесь", "хочу", "надо", "нужно", "можно",
    "проект", "проекту", "работа", "работу", "работе", "работаем", "работать",
    # English basics
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "at",
    "for", "with", "by", "from", "and", "or", "but", "not", "this", "that",
    "these", "those", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "should", "could", "may", "might", "can",
    # English continuation / meta
    "continue", "resume", "let", "lets", "now", "please", "help", "make", "do",
    "next", "more", "still", "also", "project",
    # Question words (RU + EN) — alone they're not actionable
    "what", "when", "where", "why", "how", "who", "which",
    "что", "когда", "где", "почему", "зачем", "кто", "какой", "какая", "какие",
])


def _content_tokens(query: str) -> list[str]:
    """Extract content tokens (>= 3 chars, not stopwords) from a query."""
    tokens = re.findall(r"\b[\w-]{3,}\b", query.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def is_low_confidence_query(query: str, min_content_tokens: int = 1) -> bool:
    """Detect generic / continuation / meta queries that won't yield meaningful RAG hits.

    A query is low-confidence when it has ZERO content tokens — only stopwords
    or short noise like "ok", "да". A single specific token ("nginx",
    "memory-compiler") is enough signal to attempt retrieval — Web UI search
    bar typically sends single-word queries.

    Examples flagged (0 content tokens after filtering stopwords + short noise):
      - "let's continue" / "давай продолжим"
      - "what's next?"
      - "help me"
      - "ok"

    Examples NOT flagged (≥1 content token):
      - "nginx"
      - "memory-compiler"
      - "nginx ssl prod config"
      - "deploy backend service"
    """
    if not query or not query.strip():
        return True
    content = _content_tokens(query)
    return len(content) < min_content_tokens

import numpy as np
from sentence_transformers import SentenceTransformer, CrossEncoder

from whoosh import index as whoosh_index
from whoosh.qparser import MultifieldParser, OrGroup, AndGroup, FuzzyTermPlugin
from whoosh.scoring import BM25F

from memory_compiler.config import (
    KNOWLEDGE_DIR, INDEX_DIR, PROJECTS, SCHEMA,
    decay_factor, atomic_write_bytes, is_secret_article,
)
from memory_compiler.storage import make_preview, article_body_lines
import threading as _threading
import hashlib as _hashlib

# Единый лок целостности индекса/эмбеддингов: сериализует мутации _embeddings, запись
# pickle и работу Whoosh writer'а между фоновым reindex (демон-поток) и обработчиками
# на event loop. RLock — реентрантный. Закрывает: торн-райт pickle, LockError двух
# writer'ов, lost-update при свопе, RuntimeError «dict changed size» при итерации.
_index_lock = _threading.RLock()


def snapshot_embeddings() -> dict:
    """Копия _embeddings под локом — для безопасной итерации читателями
    (semantic_search, lint, graph) пока фон/embed_document мутируют оригинал."""
    with _index_lock:
        return dict(_embeddings)

# ─── Semantic search ─────────────────────────────────────────────────────────

EMBEDDINGS_PATH = KNOWLEDGE_DIR / ".embeddings.pkl"
# Default: legacy MiniLM (384 dim) — backward compat for existing .embeddings.pkl.
# Recommended upgrade: EMBED_MODEL=BAAI/bge-m3 (1024 dim, MTEB +13, multilingual)
# or EMBED_MODEL=Alibaba-NLP/gte-multilingual-base. Cache auto-invalidates on change.
import os as _os_embed
EMBED_MODEL_NAME = _os_embed.environ.get("EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")

# Late chunking (Jina AI 2024 pattern, pragmatic variant): encode whole document as one
# embedding instead of splitting on ### sections. Preserves anaphoric refs and cross-section
# context. Best with long-context models (BGE-M3 max=8192); on MiniLM (max=128) it may
# truncate long articles — only enable when paired with EMBED_MODEL upgrade.
LATE_CHUNKING = _os_embed.environ.get("LATE_CHUNKING", "false").lower() in ("1", "true", "yes")

# Memory-safe encoding controls for big models (BGE-M3, Qwen3-Embedding etc):
# without these, model.encode(docs=540, seq=8192, hidden=1024) tries a peak
# allocation ~18GB and OOMs on NAS-class machines even with 32GB RAM.
EMBED_BATCH_SIZE = int(_os_embed.environ.get("EMBED_BATCH_SIZE", "8"))
EMBED_MAX_SEQ_LENGTH = int(_os_embed.environ.get("EMBED_MAX_SEQ_LENGTH", "2048"))

# SPLADE 3-way hybrid (opt-in). When true, whoosh_search adds a sparse-learned channel
# to the RRF merge alongside BM25 and dense embeddings. Falls back to 2-way if the
# model is unavailable (no good multilingual SPLADE published yet — keep disabled
# until that lands). When enabled, pip-install naver/splade-cocondenser-* or similar
# and override _splade_search() with a real implementation.
SPLADE_ENABLED = _os_embed.environ.get("SPLADE_ENABLED", "false").lower() in ("1", "true", "yes")


def _splade_search(query: str, project: str = "all", limit: int = 20) -> dict[str, float]:
    """Sparse-learned retrieval channel (stub).

    Returns {path: score} ranked by SPLADE relevance. Current implementation is a
    no-op stub that returns empty dict — RRF will gracefully degrade to 2-way hybrid.
    Replace with a real backend (e.g. transformers SPLADE model) when a strong
    multilingual checkpoint becomes available.
    """
    return {}
_embed_model: Optional[SentenceTransformer] = None
_embeddings: dict[str, np.ndarray] = {}  # path -> embedding
_embed_texts: dict[str, str] = {}  # path -> title+tags for display
# chunk_key -> sha1(chunk_text): позволяет rebuild_embeddings пере-кодировать ТОЛЬКО
# изменившиеся чанки (encode всей базы e5-base на ARM ~35-40 мин, инкрементально — секунды).
_chunk_hashes: dict[str, str] = {}
# Журнал конкурентных изменений на время долгого encode в rebuild_embeddings: статьи,
# сохранённые (dirty) или удалённые (deleted) ПОКА шла пересборка. При свопе rebuild
# накатывает их поверх свежесобранных диктов — иначе сохранённая статья теряется до
# следующей пересборки, а удалённая «воскресает». Мутации — только под _index_lock.
_dirty_parents: set[str] = set()
_deleted_parents: set[str] = set()


def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
        # Cap context length — long-context defaults (8192) make peak memory
        # allocation explode during batch encoding. 2048 covers >99% of our
        # articles; longer ones are truncated rather than OOM the host.
        try:
            if _embed_model.max_seq_length > EMBED_MAX_SEQ_LENGTH:
                _embed_model.max_seq_length = EMBED_MAX_SEQ_LENGTH
        except Exception:
            pass
    return _embed_model


def _needs_e5_prefix() -> bool:
    """e5-семейство (intfloat/*-e5-*) ТРЕБУЕТ асимметричных префиксов
    'query: ' / 'passage: '. Без них косинус-сходство сжато вверх (слабо
    связанные тексты дают ~0.78–0.88) — деградирует semantic_search и
    переполняет окно кросс-рефа. Прочие модели (MiniLM, BGE-M3, GTE)
    префиксы НЕ используют."""
    return "e5" in EMBED_MODEL_NAME.lower()


def encode_passages(texts: list[str]):
    """Закодировать документы/чанки (passage-сторона) с нужным префиксом."""
    model = get_embed_model()
    if _needs_e5_prefix():
        texts = [f"passage: {t}" for t in texts]
    return model.encode(
        texts, normalize_embeddings=True, show_progress_bar=False,
        batch_size=EMBED_BATCH_SIZE,
    )


def encode_query(text: str):
    """Закодировать поисковый запрос/тему (query-сторона) с нужным префиксом."""
    model = get_embed_model()
    if _needs_e5_prefix():
        text = f"query: {text}"
    return model.encode([text], normalize_embeddings=True)[0]


# ─── Cross-encoder reranker (lazy load) ────────────────────────────────────

import os as _os
# Default: BAAI/bge-reranker-v2-m3 — multilingual (built on BGE-M3), strong RU+EN.
# Override via RERANKER_MODEL env var (e.g. cross-encoder/ms-marco-MiniLM-L-6-v2 for
# RAM-constrained NAS deployments).
RERANKER_MODEL_NAME = _os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
_reranker_model: Optional[CrossEncoder] = None


def get_reranker_model() -> Optional[CrossEncoder]:
    """Lazy-load cross-encoder. Returns None on failure (graceful degradation)."""
    global _reranker_model
    if _reranker_model is None:
        try:
            _reranker_model = CrossEncoder(RERANKER_MODEL_NAME, max_length=512)
        except Exception as e:
            print(f"Reranker load failed: {e}, falling back to bi-encoder only")
            _reranker_model = False  # marker: tried and failed
    return _reranker_model if _reranker_model else None


def rerank(query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
    """Rerank candidates by cross-encoder. Each candidate dict needs 'preview' or 'title'.
    Adds 'rerank_score' field. Returns top_k sorted by it. Falls back to original order on failure.
    """
    if not candidates or len(candidates) <= 1:
        return candidates[:top_k]

    from memory_compiler import obs
    model = get_reranker_model()
    if model is None:
        obs.set_semantic_degraded(True)  # reranker недоступен → деградация до hybrid-порядка
        return candidates[:top_k]  # graceful: no reranker, keep original order

    pairs = []
    for c in candidates:
        # Use title + preview snippet (max 400 chars total)
        text = (c.get("title", "") + " " + c.get("preview", ""))[:400]
        pairs.append([query, text])

    try:
        scores = model.predict(pairs, show_progress_bar=False)
        for c, s in zip(candidates, scores):
            c["rerank_score"] = float(s)
        obs.set_semantic_degraded(False)  # reranker жив
        return sorted(candidates, key=lambda c: c.get("rerank_score", 0), reverse=True)[:top_k]
    except Exception as e:
        print(f"Rerank failed: {e}")
        obs.set_semantic_degraded(True)
        return candidates[:top_k]


def _doc_text_for_embedding(text: str) -> str:
    """Extract meaningful text for embedding (title + tags + first 500 chars of body).
    Тело — содержательные строки (article_body_lines): раньше lines[:30] включали
    шапку-метаданные и пустые строки, съедавшие бюджет 500 символов (issue #1)."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else ""
    tags = ""
    for line in lines[:10]:
        if line.lower().startswith("**теги:**"):
            tags = line.split(":", 1)[1].strip()
            break
    body_preview = " ".join(article_body_lines(text, limit=40))[:500]
    return f"{title} {tags} {body_preview}"


def _chunk_article(text: str, path_key: str) -> list[tuple[str, str]]:
    """Split article into chunks by ### sections. Returns [(chunk_key, chunk_text), ...]."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else ""
    tags = ""
    for line in lines[:10]:
        if line.lower().startswith("**теги:**"):
            tags = line.split(":", 1)[1].strip()
            break

    # Late chunking mode: return whole document as one embedding key.
    # Preserves cross-section context (anaphoric refs, headers without bodies).
    if LATE_CHUNKING:
        whole = f"{title} {tags} {text}"
        return [(path_key, whole)]

    # Find ### sections
    sections = []
    current_lines = []
    current_header = ""
    for line in lines:
        if line.startswith("### ") and current_lines:
            sections.append((current_header, "\n".join(current_lines)))
            current_lines = [line]
            current_header = line[4:].strip()
        else:
            current_lines.append(line)
            if not current_header and line.startswith("### "):
                current_header = line[4:].strip()
    if current_lines:
        sections.append((current_header, "\n".join(current_lines)))

    # If no ### sections or only 1, return single chunk
    if len(sections) <= 1:
        return [(path_key, f"{title} {tags} {' '.join(article_body_lines(text, limit=40))[:500]}")]

    # Multiple sections — create chunk per section, prepend title+tags for context
    chunks = []
    prefix = f"{title} {tags}"
    for i, (header, body) in enumerate(sections):
        chunk_key = f"{path_key}#chunk{i}"
        chunk_text = f"{prefix} {header} {body[:400]}"
        chunks.append((chunk_key, chunk_text))
    return chunks


def _chunk_hash(text: str) -> str:
    """Хэш текста чанка — ключ инкрементальности rebuild_embeddings."""
    return _hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def _persist_embeddings_locked():
    """Записать .embeddings.pkl атомарно. Вызывать ТОЛЬКО под _index_lock."""
    atomic_write_bytes(EMBEDDINGS_PATH, pickle.dumps({
        "model": EMBED_MODEL_NAME,
        "late_chunking": LATE_CHUNKING,
        "embeddings": _embeddings,
        "texts": _embed_texts,
        "chunk_hashes": _chunk_hashes,
    }))


def persist_embeddings():
    """Публичная точка персиста pkl (берёт лок сама) — для батч-операций
    (remove_project), где remove_embedding вызывается с persist=False в цикле."""
    with _index_lock:
        _persist_embeddings_locked()


def remove_embedding(parent_key: str, persist: bool = True):
    """Удалить эмбеддинги статьи (parent + все #chunkN) из индекса и pkl.

    Единственная корректная точка удаления: под _index_lock и с журналированием
    в _deleted_parents — фоновая rebuild_embeddings, уже прочитавшая файл с диска,
    при свопе выкинет статью, а не вернёт её «зомби». Вызывать из delete_article /
    remove_project вместо ручных .pop по _embeddings.
    """
    with _index_lock:
        for k in [k for k in _embeddings if k == parent_key or k.startswith(parent_key + "#")]:
            _embeddings.pop(k, None)
            _chunk_hashes.pop(k, None)
        _embed_texts.pop(parent_key, None)
        _dirty_parents.discard(parent_key)
        _deleted_parents.add(parent_key)
        if persist:
            _persist_embeddings_locked()


def rebuild_embeddings():
    """Rebuild embeddings from knowledge files with chunking. Возвращает общее
    число документов/чанков в индексе.

    Атомарность: новые дикты собираются локально и свопятся в глобалы ТОЛЬКО после
    успешного encode + pickle. Упавший encode (OOM и т.п.) оставляет прежние
    _embeddings — semantic search продолжает работать на устаревших данных.

    Инкрементальность: чанк пере-кодируется только если его sha1 изменился
    (_chunk_hashes); остальные вектора реюзаются из текущего кэша. Полная база
    e5-base на ARM — ~35-40 мин, инкрементально при 1-2 изменённых статьях — секунды.

    Конкурентность: статьи, сохранённые/удалённые во время долгого encode
    (журнал _dirty_parents/_deleted_parents), накатываются при свопе — без этого
    свежее сохранение терялось, а удаление «воскресало».
    """
    global _embeddings, _embed_texts, _chunk_hashes
    with _index_lock:
        old_embeddings = dict(_embeddings)
        old_hashes = dict(_chunk_hashes)
        _dirty_parents.clear()
        _deleted_parents.clear()

    new_embeddings: dict[str, np.ndarray] = {}
    new_embed_texts: dict[str, str] = {}
    new_hashes: dict[str, str] = {}
    docs = []   # тексты, требующие encode (новые/изменённые)
    paths = []  # их chunk-keys
    total = 0

    def _collect(chunk_key: str, chunk_text: str):
        nonlocal total
        total += 1
        h = _chunk_hash(chunk_text)
        new_hashes[chunk_key] = h
        if old_hashes.get(chunk_key) == h and chunk_key in old_embeddings:
            new_embeddings[chunk_key] = old_embeddings[chunk_key]  # реюз, без encode
        else:
            docs.append(chunk_text)
            paths.append(chunk_key)

    for proj in PROJECTS:
        p = KNOWLEDGE_DIR / proj
        if not p.exists():
            continue
        for md in p.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue  # skip unreadable/binary files
            text = _index_safe_text(text, md.name)
            key = f"{proj}/{md.name}"
            lines = text.splitlines()
            new_embed_texts[key] = lines[0].lstrip("# ").strip() if lines else md.stem
            # Chunk article for finer-grained search
            for chunk_key, chunk_text in _chunk_article(text, key):
                _collect(chunk_key, chunk_text)

    daily = KNOWLEDGE_DIR / "daily"
    if daily.exists():
        for md in daily.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            key = f"daily/{md.name}"
            _collect(key, _doc_text_for_embedding(_index_safe_text(text, md.name)))
            new_embed_texts[key] = md.stem

    if docs:
        # Batch in small chunks — long-context models (BGE-M3) blow up peak
        # allocation when encoding hundreds of long docs at once.
        vectors = encode_passages(docs)
        for i, path in enumerate(paths):
            new_embeddings[path] = vectors[i]
        print(f"rebuild_embeddings: encoded {len(docs)}, reused {total - len(docs)}")

    # Atomic swap + pickle под локом: коммитим глобалы и пишем pickle ТОЛЬКО после
    # успешного encode, и так, чтобы конкурентный embed_document (event loop) не
    # пересёкся со свопом/записью (торн-райт / lost-update). Запись — atomic_write_bytes.
    with _index_lock:
        # Накат конкурентных изменений времён encode: сохранённое во время пересборки
        # свежее прочитанного с диска, удалённое — не должно вернуться со свопом.
        for parent in _dirty_parents:
            # Сперва выкинуть ВСЕ ключи родителя из свежесобранных: если статья за время
            # encode стала односекционной (меньше чанков), старые parent#chunkN остались бы
            # зомби (матчились бы по удалённому содержимому). Симметрично embed_document.
            for k in [k for k in new_embeddings if k == parent or k.startswith(parent + "#")]:
                new_embeddings.pop(k, None)
                new_hashes.pop(k, None)
            # Затем накатить актуальные вектора из живого _embeddings.
            for k, v in _embeddings.items():
                if k == parent or k.startswith(parent + "#"):
                    new_embeddings[k] = v
                    if k in _chunk_hashes:
                        new_hashes[k] = _chunk_hashes[k]
            if parent in _embed_texts:
                new_embed_texts[parent] = _embed_texts[parent]
        for parent in _deleted_parents:
            for k in [k for k in new_embeddings if k == parent or k.startswith(parent + "#")]:
                new_embeddings.pop(k, None)
                new_hashes.pop(k, None)
            new_embed_texts.pop(parent, None)
        _dirty_parents.clear()
        _deleted_parents.clear()
        _embeddings = new_embeddings
        _embed_texts = new_embed_texts
        _chunk_hashes = new_hashes
        _persist_embeddings_locked()

    return total


def load_embeddings():
    """Load embeddings from disk if available.

    Returns False if the cached pkl was produced by a different embedding model
    (different dimensionality / different semantics) — caller should rebuild.
    Logs the reason for visibility instead of failing silently.
    """
    global _embeddings, _embed_texts, _chunk_hashes
    if not EMBEDDINGS_PATH.exists():
        return False
    try:
        with open(EMBEDDINGS_PATH, "rb") as f:
            data = pickle.load(f)
    except Exception as e:
        print(f"load_embeddings: pkl corrupt or unreadable ({e}) — will rebuild")
        return False
    cached_model = data.get("model") if isinstance(data, dict) else None
    if cached_model != EMBED_MODEL_NAME:
        print(f"load_embeddings: model mismatch (pkl={cached_model!r} vs "
              f"current={EMBED_MODEL_NAME!r}) — will rebuild")
        return False
    # LATE_CHUNKING flag changes the embedding TOPOLOGY (whole-doc vs per-section
    # chunks). When the flag flips, the cache becomes invalid even though the
    # model didn't change. Legacy pkl without 'late_chunking' key is treated as
    # current value (no invalidation) — preserves cache for users on the same flag.
    cached_late = data.get("late_chunking", LATE_CHUNKING)
    if cached_late != LATE_CHUNKING:
        print(f"load_embeddings: LATE_CHUNKING mismatch (pkl={cached_late} vs "
              f"current={LATE_CHUNKING}) — will rebuild")
        return False
    try:
        _embeddings = data["embeddings"]
        _embed_texts = data["texts"]
        # Legacy pkl без chunk_hashes → пустой дикт: первый rebuild пере-кодирует
        # всё (нет ложного реюза), дальше кэш хэшей живёт в pkl.
        _chunk_hashes = data.get("chunk_hashes") or {}
        return True
    except (KeyError, TypeError) as e:
        print(f"load_embeddings: pkl schema invalid ({e}) — will rebuild")
        return False


def embed_document(text: str, filename: str, project: str):
    """Add/update embedding(s) for a single document using the same chunking
    strategy as rebuild_embeddings (so freshly-saved articles match the rest
    of the index — no second-class representation)."""
    global _embeddings, _embed_texts
    model = get_embed_model()
    text = _index_safe_text(text, filename)
    parent_key = f"{project}/{filename}"
    lines = text.splitlines()
    chunks = _chunk_article(text, parent_key)
    chunk_texts = [c[1] for c in chunks]
    vectors = encode_passages(chunk_texts)  # encode вне лока — может быть долгим

    # Мутация _embeddings + запись pickle — под локом и атомарно (tmp+os.replace),
    # чтобы не пересечься с фоновым rebuild_embeddings (торн-райт / lost-update).
    with _index_lock:
        _embed_texts[parent_key] = lines[0].lstrip("# ").strip() if lines else filename
        # Remove any prior chunks for this article (chunking topology may have changed)
        for old_key in list(_embeddings.keys()):
            if old_key == parent_key or old_key.startswith(parent_key + "#"):
                _embeddings.pop(old_key, None)
                _chunk_hashes.pop(old_key, None)
        for (chunk_key, chunk_text), vec in zip(chunks, vectors):
            _embeddings[chunk_key] = vec
            _chunk_hashes[chunk_key] = _chunk_hash(chunk_text)
        # Журнал для идущей параллельно rebuild_embeddings: эта статья свежее
        # прочитанного ею с диска — при свопе её нужно сохранить, не потерять.
        _dirty_parents.add(parent_key)
        _deleted_parents.discard(parent_key)
        _persist_embeddings_locked()


def semantic_search(query: str, limit: int = 10) -> list[tuple[str, float]]:
    """Search by semantic similarity. Returns [(path, score), ...]. Deduplicates chunks to parent articles.

    Векторизовано: вместо Python-цикла np.dot по каждому вектору (+ отдельная копия
    словаря) — один проход под локом собирает матрицу (N×d), затем одно BLAS-умножение
    M @ q. На больших базах (тысячи чанков) это 10-50× быстрее Python-цикла. Эмбеддинги
    нормализованы (encode(normalize_embeddings=True)), поэтому dot = косинус — результат
    идентичен прежней реализации (дедуп берёт max по родителю независимо от порядка)."""
    with _index_lock:  # консистентный снимок за один проход (дешевле dict-копии + цикла)
        if not _embeddings:
            return []
        keys = list(_embeddings.keys())
        matrix = np.stack([_embeddings[k] for k in keys])  # (N, d)
    q_vec = encode_query(query)  # вне лока: инференс модели медленный, матрица уже снята
    sims = matrix @ q_vec  # (N,) — одно матрично-векторное умножение вместо N np.dot
    # Deduplicate: keep best score per parent article (strip #chunkN)
    seen: dict[str, float] = {}
    for key, sim in zip(keys, sims):
        parent = key.split("#")[0]
        sim = float(sim)
        if parent not in seen or sim > seen[parent]:
            seen[parent] = sim
    results = sorted(seen.items(), key=lambda x: x[1], reverse=True)
    return results[:limit]


# ─── Whoosh index ────────────────────────────────────────────────────────────

_ix = None  # global whoosh index

# Кросс-проектные (shared/global) статьи. Общая сущность (канал уведомлений, единый
# пароль, общий креденшл) физически лежит в ОДНОМ проекте, но нужна инфраструктурно.
# При скоупе поиска на проект такие статьи раньше не находились. Помеченные тегом
# `shared` или `global` попадают в выдачу ЛЮБОГО проекта. Множество путей
# ("project/filename") наполняется при индексации (rebuild_index/index_document) —
# там теги уже парсятся, лишних сканов диска нет. Мутации под _index_lock.
_SHARED_TAG_MARKERS = frozenset({"shared", "global", "общий", "общая", "общее"})
_shared_paths: set[str] = set()


def _tags_are_shared(tags: str) -> bool:
    """True, если среди тегов есть маркер кросс-проектности (shared/global/общий)."""
    toks = set(re.findall(r"[\wа-яё-]+", tags.lower()))
    return bool(toks & _SHARED_TAG_MARKERS)


def _index_safe_text(raw_text: str, filename: str) -> str:
    """Для секретных статей вернуть плейсхолдер (титул + теги) вместо тела — чтобы
    ЛЮБАЯ индексация (полный reindex, rebuild_embeddings, прямой index_document)
    соблюдала тот же инвариант, что save_secret/edit_article: содержимое секрета
    (включая авторские plaintext-секции) не попадает в поиск/эмбеддинги. Сам ENC:
    это шифртекст и не утечка, но и его держать в индексе незачем. Несекретные
    статьи возвращаются без изменений."""
    if not is_secret_article(raw_text, filename):
        return raw_text
    lines = raw_text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else filename
    tags_line = next((l for l in lines[:12] if l.lower().startswith("**теги:**")),
                     "**Теги:** secret")
    return f"# {title}\n\n{tags_line}\n\n[зашифрованная статья]"


def _parse_article(text: str, filename: str, project: str) -> dict:
    """Parse markdown article into whoosh fields."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else filename
    tags = ""
    for line in lines[:10]:
        if line.lower().startswith("**теги:**"):
            tags = line.split(":", 1)[1].strip().replace(",", " ")
            break
    # Preview — от ТЕЛА статьи, не от начала файла: шапка с «Обновлено» занимает
    # 10 строк и вытесняла весь контент из выдачи search (issue #1).
    preview = make_preview(text, n=10)
    return dict(title=title, tags=tags, body=text, preview=preview, project=project, path=f"{project}/{filename}")


def get_index() -> whoosh_index.Index:
    """Get or create whoosh index."""
    global _ix
    if _ix is not None:
        return _ix
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    if whoosh_index.exists_in(str(INDEX_DIR)):
        _ix = whoosh_index.open_dir(str(INDEX_DIR))
    else:
        _ix = whoosh_index.create_in(str(INDEX_DIR), SCHEMA)
        rebuild_index()
    return _ix


# ─── Background reindex ────────────────────────────────────────────────────
_reindex_lock = _threading.Lock()
_reindex_running = {"v": False}


def reindex_running() -> bool:
    return _reindex_running["v"]


def start_background_reindex() -> bool:
    """Запустить полный reindex (BM25F + embeddings + index.md) в демон-потоке.
    Возвращает True если запущен, False если reindex уже идёт. НЕ блокирует
    event loop — сервер остаётся отзывчивым (раньше синхронный reindex вешал
    сервер на ~26 мин, /api/health не отвечал)."""
    if not _reindex_lock.acquire(blocking=False):
        return False
    _reindex_running["v"] = True

    def _run():
        try:
            rebuild_index()
            rebuild_embeddings()
            try:
                from memory_compiler.storage import regenerate_index
                regenerate_index()
            except Exception:
                pass
        finally:
            _reindex_running["v"] = False
            _reindex_lock.release()

    _threading.Thread(target=_run, daemon=True).start()
    return True


def rebuild_index():
    """Полная переиндексация всех knowledge-файлов — НЕДЕСТРУКТИВНО.

    Раньше делала create_in (пересоздавала ПУСТОЙ индекс) → на всё время наполнения
    индекс был пуст (blackout: поиск возвращал ничего), и прерывание оставляло пустой
    индекс. Теперь обновляет существующий индекс через update_document (add/replace по
    unique path) и удаляет устаревшие документы (были в индексе, но исчезли с диска).
    Изменения буферизуются writer'ом и коммитятся АТОМАРНО — читатели видят прежнюю
    полную версию индекса до commit, затем новую полную; пустого окна нет. Кросс-
    платформенно (без переименования каталогов, которое ломается на Windows при
    открытых файлах индекса).

    Схема считается стабильной: при изменении SCHEMA нужен холодный пересбор (удалить
    каталог индекса → get_index соберёт заново на старте)."""
    global _shared_paths
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    ix = get_index()  # существующий с диска или пустой (create_in на холодном старте)
    # Под локом: writer не пересекается с index_document/фоновым rebuild (event loop) —
    # два writer'а на одном каталоге дают Whoosh LockError, рушивший save_lesson.
    with _index_lock:
        # Текущие path в индексе — чтобы вычислить устаревшие (снапшот до записи).
        with ix.reader() as reader:
            old_paths = {sf.get("path") for sf in reader.all_stored_fields()}
        old_paths.discard(None)

        writer = ix.writer()
        new_shared: set[str] = set()
        seen: set[str] = set()
        count = 0

        def _index_dir(proj_name: str, dir_path):
            nonlocal count
            for md in dir_path.glob("*.md"):
                # _index_safe_text: секрет (маркер '**Секрет:** да') не становится
                # searchable — тело маскируется плейсхолдером (симметрично для daily).
                text = _index_safe_text(md.read_text(encoding="utf-8"), md.name)
                fields = _parse_article(text, md.name, proj_name)
                writer.update_document(**fields)  # add или replace по unique path
                seen.add(fields["path"])
                if _tags_are_shared(fields["tags"]):
                    new_shared.add(fields["path"])
                count += 1

        for proj in PROJECTS:
            p = KNOWLEDGE_DIR / proj
            if p.exists():
                _index_dir(proj, p)
        daily = KNOWLEDGE_DIR / "daily"
        if daily.exists():
            _index_dir("daily", daily)

        # Удалить документы, исчезнувшие с диска (были в индексе, но не встречены).
        for stale in old_paths - seen:
            writer.delete_by_term("path", stale)

        writer.commit()
        _shared_paths = new_shared  # атомарный своп под локом
    return count


def start_background_index_refresh() -> bool:
    """Фоновое НЕДЕСТРУКТИВНОЕ обновление Whoosh-индекса (rebuild_index через
    update_document — без blackout). Для старта: открытый с диска индекс мог устареть,
    если knowledge правили в обход index_document (bulk-edit на NAS, git pull). Не
    блокирует старт. Возвращает False, если полный reindex уже идёт."""
    if not _reindex_lock.acquire(blocking=False):
        return False
    _reindex_running["v"] = True

    def _run():
        try:
            rebuild_index()
        finally:
            _reindex_running["v"] = False
            _reindex_lock.release()

    _threading.Thread(target=_run, daemon=True).start()
    return True


def startup_prepare_index() -> int:
    """Старт-подготовка индекса. Если индекс уже на диске — открыть его (быстро) и
    догнать внешние правки НЕДЕСТРУКТИВНЫМ фоновым rebuild_index (без blackout); иначе
    собрать синхронно (холодный первый старт). Возвращает число документов.

    Раньше на КАЖДОМ старте шёл полный синхронный rebuild_index прямо в lifespan —
    блокировал готовность сервера и /api/health; при росте базы рисковал упереться в
    healthcheck-таймаут Docker. В v1.9.0-1 фоновое обновление на старте отключалось,
    т.к. старый rebuild_index через create_in опустошал живой индекс (blackout-окно).
    Теперь rebuild_index недеструктивен — фоновое обновление безопасно и вернулось:
    подхватывает внешние правки knowledge (bulk-edit на NAS, git pull) без blackout."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    if whoosh_index.exists_in(str(INDEX_DIR)):
        try:
            ix = get_index()  # open_dir существующего индекса
            with ix.searcher() as s:
                count = s.doc_count()
            start_background_index_refresh()  # недеструктивно — без blackout
            return count
        except Exception:
            pass  # индекс битый/неоткрываемый — пересобираем синхронно ниже
    return rebuild_index()


def index_document(text: str, filename: str, project: str):
    """Add or update a single document in the index."""
    ix = get_index()
    text = _index_safe_text(text, filename)
    fields = _parse_article(text, filename, project)
    with _index_lock:  # сериализуем writer с фоновым rebuild_index (иначе Whoosh LockError)
        writer = ix.writer()
        writer.update_document(**fields)
        writer.commit()
        # Поддерживаем _shared_paths актуальным: тег shared могли добавить/снять
        # при редактировании статьи.
        if _tags_are_shared(fields["tags"]):
            _shared_paths.add(fields["path"])
        else:
            _shared_paths.discard(fields["path"])


def delete_document(path_key: str) -> None:
    """Точечно удалить ОДИН документ из Whoosh по path (unique ID). Недеструктивно:
    не пересобирает индекс через create_in — нет blackout-окна и не блокирует event
    loop на минуты, как полный rebuild_index (была причина зависания delete_article)."""
    ix = get_index()
    with _index_lock:  # сериализуем writer с фоновым rebuild_index (иначе Whoosh LockError)
        writer = ix.writer()
        writer.delete_by_term("path", path_key)
        writer.commit()
        _shared_paths.discard(path_key)  # под локом — как index_document (своп rebuild)


def delete_project_documents(project: str) -> int:
    """Удалить ВСЕ документы проекта из Whoosh по полю project. Недеструктивно —
    для remove_project вместо полного rebuild_index. Возвращает число удалённых."""
    ix = get_index()
    prefix = f"{project}/"
    with _index_lock:
        writer = ix.writer()
        n = writer.delete_by_term("project", project)
        writer.commit()
        _shared_paths.difference_update({p for p in list(_shared_paths) if p.startswith(prefix)})
    return n


def whoosh_search(query_str: str, project: str = "all", limit: int = 10) -> list[dict]:
    """Hybrid search: BM25F keyword + semantic similarity."""
    ix = get_index()
    group = AndGroup if len(query_str.split()) > 1 else OrGroup
    parser = MultifieldParser(["title", "tags", "body"], schema=ix.schema, group=group)
    parser.add_plugin(FuzzyTermPlugin())  # fuzzy matching

    # 1. BM25F keyword search
    bm25_scores: dict[str, dict] = {}
    try:
        q = parser.parse(query_str)
    except Exception:
        q = None
    if q:
        with ix.searcher(weighting=BM25F(title_B=0.75, tags_B=0.75, body_B=0.75)) as s:
            hits = s.search(q, limit=limit * 2)
            max_bm25 = max((h.score for h in hits), default=1.0) or 1.0
            for hit in hits:
                path = hit["path"]
                # Скоуп на проект, НО shared/global-статьи из других проектов пропускаем.
                if project != "all" and hit["project"] != project and path not in _shared_paths:
                    continue
                bm25_scores[path] = {
                    "title": hit["title"],
                    "project": hit["project"],
                    "file": path.split("/", 1)[-1] if "/" in path else path,
                    "preview": hit["preview"],
                    "bm25": hit.score / max_bm25,  # normalize to 0-1
                }

    # 2. Semantic search
    sem_scores: dict[str, float] = {}
    sem_results = semantic_search(query_str, limit=limit * 2)
    for path, sim in sem_results:
        # Скоуп на проект, НО shared/global-статьи пропускаем в любой проект.
        if project != "all" and not path.startswith(project + "/") and path not in _shared_paths:
            continue
        sem_scores[path] = max(sim, 0)  # cosine sim, already 0-1

    # 2b. SPLADE sparse-learned channel (opt-in, gracefully empty if disabled)
    splade_scores: dict[str, float] = {}
    if SPLADE_ENABLED:
        try:
            splade_scores = _splade_search(query_str, project=project, limit=limit * 2)
        except Exception:
            splade_scores = {}

    # 3. Merge via Reciprocal Rank Fusion (RRF) — industry standard for hybrid retrieval.
    # Formula: score(d) = Σ_q 1 / (k + rank_q(d))
    # Не требует калибровки между BM25 и cosine, устойчив к выбросам.
    # k=60 — общепринятая константа (Cormack et al., 2009).
    RRF_K = 60

    bm25_ranked = sorted(bm25_scores.keys(),
                         key=lambda p: -bm25_scores[p]["bm25"])
    sem_ranked = sorted(sem_scores.keys(),
                        key=lambda p: -sem_scores[p])
    splade_ranked = sorted(splade_scores.keys(),
                           key=lambda p: -splade_scores[p])
    bm25_rank = {p: i + 1 for i, p in enumerate(bm25_ranked)}
    sem_rank = {p: i + 1 for i, p in enumerate(sem_ranked)}
    splade_rank = {p: i + 1 for i, p in enumerate(splade_ranked)}

    all_paths = set(bm25_scores.keys()) | set(sem_scores.keys()) | set(splade_scores.keys())
    merged = []
    for path in all_paths:
        rrf = 0.0
        if path in bm25_rank:
            rrf += 1.0 / (RRF_K + bm25_rank[path])
        if path in sem_rank:
            rrf += 1.0 / (RRF_K + sem_rank[path])
        if path in splade_rank:
            rrf += 1.0 / (RRF_K + splade_rank[path])

        if path in bm25_scores:
            info = bm25_scores[path]
        else:
            # Semantic-only result — get info from embed_texts
            proj = path.split("/", 1)[0] if "/" in path else "unknown"
            fname = path.split("/", 1)[-1] if "/" in path else path
            title = _embed_texts.get(path, fname)
            fpath = KNOWLEDGE_DIR / path
            preview = ""
            if fpath.exists():
                raw = fpath.read_text(encoding="utf-8")
                preview = make_preview(_index_safe_text(raw, fname), n=10)
            info = {"title": title, "project": proj, "file": fname, "preview": preview}

        # Apply temporal decay
        decay = decay_factor(path)
        # Scale RRF to a comparable 0..100 range:
        # max possible RRF for two-source merge ≈ 2/(K+1) = 2/61 ≈ 0.0328
        # multiply by 3000 → top result lands around 100, comfortable for thresholds.
        rrf_scaled = rrf * 3000 * (0.7 + 0.3 * decay)
        info["score"] = round(rrf_scaled, 1)
        merged.append(info)

    merged.sort(key=lambda x: x["score"], reverse=True)
    # Remove internal fields
    for m in merged:
        m.pop("bm25", None)

    if not merged:
        return []
    if is_low_confidence_query(query_str):
        return []

    top_score = merged[0]["score"]
    HIGH_CONF = 35  # confident retrieval — clean results
    LOW_CONF = 18   # soft fallback — low confidence but worth showing

    # High-confidence path: standard relative cutoff
    if top_score >= HIGH_CONF:
        threshold = max(top_score * 0.5, 32)
        return [m for m in merged if m["score"] >= threshold][:limit]

    # Soft-fallback path: top result is weak. Show up to 3 with `confidence: low`
    # marker IF they share at least one query token with title/preview.
    # Avoids returning silent emptiness when something semi-related exists.
    if top_score >= LOW_CONF:
        q_tokens = set(_content_tokens(query_str))
        if not q_tokens:
            return []
        soft_results = []
        for m in merged[:5]:
            haystack = (m.get("title", "") + " " + m.get("preview", "")).lower()
            haystack_tokens = set(re.findall(r"[\wа-яё-]{3,}", haystack))
            # Either direct token overlap OR via stems (lemma collision)
            from memory_compiler.config import _bilingual_stem
            q_stems = {_bilingual_stem(t) for t in q_tokens}
            h_stems = {_bilingual_stem(t) for t in haystack_tokens}
            if q_tokens & haystack_tokens or q_stems & h_stems:
                m_copy = dict(m)
                m_copy["confidence"] = "low"
                soft_results.append(m_copy)
                if len(soft_results) >= min(3, limit):
                    break
        return soft_results

    # Below LOW_CONF — truly nothing relevant
    return []
