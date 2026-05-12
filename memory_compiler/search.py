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
EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_embed_model: Optional[SentenceTransformer] = None
_embeddings: dict[str, np.ndarray] = {}  # path -> embedding
_embed_texts: dict[str, str] = {}  # path -> title+tags for display


def get_embed_model() -> SentenceTransformer:
    global _embed_model
    if _embed_model is None:
        _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    return _embed_model


# ─── Cross-encoder reranker (lazy load) ────────────────────────────────────

RERANKER_MODEL_NAME = "BAAI/bge-reranker-base"
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
    """Rebuild all embeddings from knowledge files with chunking."""
    global _embeddings, _embed_texts
    model = get_embed_model()
    _embeddings = {}
    _embed_texts = {}
    docs = []
    paths = []

    for proj in PROJECTS:
        p = KNOWLEDGE_DIR / proj
        if not p.exists():
            continue
        for md in p.glob("*.md"):
            text = md.read_text(encoding="utf-8")
            key = f"{proj}/{md.name}"
            lines = text.splitlines()
            _embed_texts[key] = lines[0].lstrip("# ").strip() if lines else md.stem
            # Chunk article for finer-grained search
            chunks = _chunk_article(text, key)
            for chunk_key, chunk_text in chunks:
                docs.append(chunk_text)
                paths.append(chunk_key)

    daily = KNOWLEDGE_DIR / "daily"
    if daily.exists():
        for md in daily.glob("*.md"):
            text = md.read_text(encoding="utf-8")
            key = f"daily/{md.name}"
            docs.append(_doc_text_for_embedding(text))
            paths.append(key)
            _embed_texts[key] = md.stem

    if docs:
        vectors = model.encode(docs, show_progress_bar=False, normalize_embeddings=True)
        for i, path in enumerate(paths):
            _embeddings[path] = vectors[i]

    # Save to disk for faster restart
    with open(EMBEDDINGS_PATH, "wb") as f:
        pickle.dump({"embeddings": _embeddings, "texts": _embed_texts}, f)

    return len(docs)


def load_embeddings():
    """Load embeddings from disk if available."""
    global _embeddings, _embed_texts
    if EMBEDDINGS_PATH.exists():
        try:
            with open(EMBEDDINGS_PATH, "rb") as f:
                data = pickle.load(f)
                _embeddings = data["embeddings"]
                _embed_texts = data["texts"]
                return True
        except Exception:
            pass
    return False


def embed_document(text: str, filename: str, project: str):
    """Add embedding for a single new document."""
    global _embeddings, _embed_texts
    model = get_embed_model()
    doc_text = _doc_text_for_embedding(text)
    key = f"{project}/{filename}"
    vec = model.encode([doc_text], normalize_embeddings=True)[0]
    _embeddings[key] = vec
    lines = text.splitlines()
    _embed_texts[key] = lines[0].lstrip("# ").strip() if lines else filename
    # Save updated embeddings
    with open(EMBEDDINGS_PATH, "wb") as f:
        pickle.dump({"embeddings": _embeddings, "texts": _embed_texts}, f)


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

    # 3. Merge via Reciprocal Rank Fusion (RRF) — industry standard for hybrid retrieval.
    # Formula: score(d) = Σ_q 1 / (k + rank_q(d))
    # Не требует калибровки между BM25 и cosine, устойчив к выбросам.
    # k=60 — общепринятая константа (Cormack et al., 2009).
    RRF_K = 60

    bm25_ranked = sorted(bm25_scores.keys(),
                         key=lambda p: -bm25_scores[p]["bm25"])
    sem_ranked = sorted(sem_scores.keys(),
                        key=lambda p: -sem_scores[p])
    bm25_rank = {p: i + 1 for i, p in enumerate(bm25_ranked)}
    sem_rank = {p: i + 1 for i, p in enumerate(sem_ranked)}

    all_paths = set(bm25_scores.keys()) | set(sem_scores.keys())
    merged = []
    for path in all_paths:
        rrf = 0.0
        if path in bm25_rank:
            rrf += 1.0 / (RRF_K + bm25_rank[path])
        if path in sem_rank:
            rrf += 1.0 / (RRF_K + sem_rank[path])

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
