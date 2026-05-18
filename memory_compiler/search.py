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
])


def _content_tokens(query: str) -> list[str]:
    """Extract content tokens (>= 3 chars, not stopwords) from a query."""
    tokens = re.findall(r"\b[\w-]{3,}\b", query.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def is_low_confidence_query(query: str, min_content_tokens: int = 2) -> bool:
    """Detect generic / continuation / meta queries that won't yield meaningful RAG hits.

    Examples flagged:
      - "let's continue" / "давай продолжим"
      - "what's next?"
      - "help me"
      - "ok"

    Examples NOT flagged:
      - "nginx ssl prod config"
      - "POST /v1/orders endpoint"
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
    decay_factor,
)

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

    model = get_reranker_model()
    if model is None:
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
        return sorted(candidates, key=lambda c: c.get("rerank_score", 0), reverse=True)[:top_k]
    except Exception as e:
        print(f"Rerank failed: {e}")
        return candidates[:top_k]


def _doc_text_for_embedding(text: str) -> str:
    """Extract meaningful text for embedding (title + tags + first 500 chars of body)."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else ""
    tags = ""
    for line in lines[:10]:
        if line.lower().startswith("**теги:**"):
            tags = line.split(":", 1)[1].strip()
            break
    body_preview = " ".join(lines[:30])[:500]
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
        return [(path_key, f"{title} {tags} {' '.join(lines[:30])[:500]}")]

    # Multiple sections — create chunk per section, prepend title+tags for context
    chunks = []
    prefix = f"{title} {tags}"
    for i, (header, body) in enumerate(sections):
        chunk_key = f"{path_key}#chunk{i}"
        chunk_text = f"{prefix} {header} {body[:400]}"
        chunks.append((chunk_key, chunk_text))
    return chunks


def rebuild_embeddings():
    """Rebuild all embeddings from knowledge files with chunking.

    Atomic: builds new dicts locally and only swaps them into the globals AFTER
    successful encode + pickle dump. If encode raises (OOM, network, etc.), the
    previous in-memory _embeddings stays intact — semantic search keeps working
    with stale-but-functional data instead of silently returning empty results.
    """
    global _embeddings, _embed_texts
    model = get_embed_model()
    new_embeddings: dict[str, np.ndarray] = {}
    new_embed_texts: dict[str, str] = {}
    docs = []
    paths = []

    for proj in PROJECTS:
        p = KNOWLEDGE_DIR / proj
        if not p.exists():
            continue
        for md in p.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue  # skip unreadable/binary files
            key = f"{proj}/{md.name}"
            lines = text.splitlines()
            new_embed_texts[key] = lines[0].lstrip("# ").strip() if lines else md.stem
            # Chunk article for finer-grained search
            chunks = _chunk_article(text, key)
            for chunk_key, chunk_text in chunks:
                docs.append(chunk_text)
                paths.append(chunk_key)

    daily = KNOWLEDGE_DIR / "daily"
    if daily.exists():
        for md in daily.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            key = f"daily/{md.name}"
            docs.append(_doc_text_for_embedding(text))
            paths.append(key)
            new_embed_texts[key] = md.stem

    if docs:
        # Batch in small chunks — long-context models (BGE-M3) blow up peak
        # allocation when encoding hundreds of long docs at once.
        vectors = model.encode(
            docs,
            show_progress_bar=False,
            normalize_embeddings=True,
            batch_size=EMBED_BATCH_SIZE,
        )
        for i, path in enumerate(paths):
            new_embeddings[path] = vectors[i]

    # Atomic swap: only commit globals AFTER successful encode.
    _embeddings = new_embeddings
    _embed_texts = new_embed_texts

    # Save to disk for faster restart. Tag with model_name — load invalidates on mismatch.
    with open(EMBEDDINGS_PATH, "wb") as f:
        pickle.dump({
            "model": EMBED_MODEL_NAME,
            "late_chunking": LATE_CHUNKING,
            "embeddings": _embeddings,
            "texts": _embed_texts,
        }, f)

    return len(docs)


def load_embeddings():
    """Load embeddings from disk if available.

    Returns False if the cached pkl was produced by a different embedding model
    (different dimensionality / different semantics) — caller should rebuild.
    Logs the reason for visibility instead of failing silently.
    """
    global _embeddings, _embed_texts
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
    parent_key = f"{project}/{filename}"
    lines = text.splitlines()
    _embed_texts[parent_key] = lines[0].lstrip("# ").strip() if lines else filename

    # Remove any prior chunks for this article (chunking topology may have changed)
    for old_key in list(_embeddings.keys()):
        if old_key == parent_key or old_key.startswith(parent_key + "#"):
            _embeddings.pop(old_key, None)

    chunks = _chunk_article(text, parent_key)
    chunk_texts = [c[1] for c in chunks]
    vectors = model.encode(
        chunk_texts,
        normalize_embeddings=True,
        show_progress_bar=False,
        batch_size=EMBED_BATCH_SIZE,
    )
    for (chunk_key, _), vec in zip(chunks, vectors):
        _embeddings[chunk_key] = vec

    # Save updated embeddings (with model tag for cache invalidation)
    with open(EMBEDDINGS_PATH, "wb") as f:
        pickle.dump({
            "model": EMBED_MODEL_NAME,
            "late_chunking": LATE_CHUNKING,
            "embeddings": _embeddings,
            "texts": _embed_texts,
        }, f)


def semantic_search(query: str, limit: int = 10) -> list[tuple[str, float]]:
    """Search by semantic similarity. Returns [(path, score), ...]. Deduplicates chunks to parent articles."""
    if not _embeddings:
        return []
    model = get_embed_model()
    q_vec = model.encode([query], normalize_embeddings=True)[0]
    raw_scores = []
    for path, vec in _embeddings.items():
        sim = float(np.dot(q_vec, vec))
        raw_scores.append((path, sim))
    raw_scores.sort(key=lambda x: x[1], reverse=True)
    # Deduplicate: keep best score per parent article (strip #chunkN)
    seen = {}
    for path, sim in raw_scores:
        parent = path.split("#")[0]
        if parent not in seen or sim > seen[parent]:
            seen[parent] = sim
    results = sorted(seen.items(), key=lambda x: x[1], reverse=True)
    return results[:limit]


# ─── Whoosh index ────────────────────────────────────────────────────────────

_ix = None  # global whoosh index


def _parse_article(text: str, filename: str, project: str) -> dict:
    """Parse markdown article into whoosh fields."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else filename
    tags = ""
    for line in lines[:10]:
        if line.lower().startswith("**теги:**"):
            tags = line.split(":", 1)[1].strip().replace(",", " ")
            break
    preview = "\n".join(lines[:10])
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


def rebuild_index():
    """Full reindex of all knowledge files."""
    global _ix
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _ix = whoosh_index.create_in(str(INDEX_DIR), SCHEMA)
    writer = _ix.writer()
    count = 0
    for proj in PROJECTS:
        p = KNOWLEDGE_DIR / proj
        if not p.exists():
            continue
        for md in p.glob("*.md"):
            text = md.read_text(encoding="utf-8")
            fields = _parse_article(text, md.name, proj)
            writer.add_document(**fields)
            count += 1
    # Also index daily logs
    daily = KNOWLEDGE_DIR / "daily"
    if daily.exists():
        for md in daily.glob("*.md"):
            text = md.read_text(encoding="utf-8")
            fields = _parse_article(text, md.name, "daily")
            writer.add_document(**fields)
            count += 1
    writer.commit()
    return count


def index_document(text: str, filename: str, project: str):
    """Add or update a single document in the index."""
    ix = get_index()
    fields = _parse_article(text, filename, project)
    writer = ix.writer()
    writer.update_document(**fields)
    writer.commit()


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
                if project != "all" and hit["project"] != project:
                    continue
                path = hit["path"]
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
        if project != "all" and not path.startswith(project + "/"):
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
                lines = fpath.read_text(encoding="utf-8").splitlines()[:20]
                preview = "\n".join(lines)
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
