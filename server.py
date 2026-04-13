"""
memory-compiler MCP server — SSE transport
Гибридный поиск: Whoosh BM25F + sentence-transformers semantic search.
"""
import asyncio
import json
import os
import re
import pickle
import subprocess
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

import numpy as np
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from sentence_transformers import SentenceTransformer

from whoosh import index as whoosh_index
from whoosh.fields import Schema, TEXT, ID, STORED, DATETIME
from whoosh.analysis import RegexTokenizer, LowercaseFilter
from whoosh.qparser import MultifieldParser, OrGroup, AndGroup, FuzzyTermPlugin
from whoosh.scoring import BM25F

KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR", "/knowledge"))
INDEX_DIR = KNOWLEDGE_DIR / ".whoosh_index"
_INITIAL_PROJECTS = os.environ.get("PROJECTS", "general").split(",")
_HIDDEN_DIRS = {".whoosh_index", ".git", "daily"}


def _discover_projects() -> list[str]:
    """Собрать список проектов из существующих папок + initial."""
    found = set(_INITIAL_PROJECTS)
    if KNOWLEDGE_DIR.exists():
        for d in KNOWLEDGE_DIR.iterdir():
            if d.is_dir() and d.name not in _HIDDEN_DIRS and not d.name.startswith("."):
                found.add(d.name)
    return sorted(found)


# Динамический список — обновляется при добавлении/удалении
PROJECTS = _discover_projects()

# Custom analyzer: splits on dots, @, spaces etc. Works with cyrillic+latin.
analyzer = RegexTokenizer(r'[\w]{2,}') | LowercaseFilter()
SCHEMA = Schema(
    path=ID(stored=True, unique=True),
    project=ID(stored=True),
    title=TEXT(stored=True, analyzer=analyzer, field_boost=5.0),
    tags=TEXT(stored=True, analyzer=analyzer, field_boost=3.0),
    body=TEXT(analyzer=analyzer, field_boost=1.0),
    preview=STORED,
)

app = Server("memory-compiler")
_ix = None  # global whoosh index

# Usage stats
_stats = {"search": 0, "save": 0, "get_context": 0, "compile": 0, "lint": 0, "total_chars_returned": 0}

# ─── Article metadata (temporal decay + analytics) ───────────────────────────

ARTICLE_META_PATH = KNOWLEDGE_DIR / ".article_meta.json"
_article_meta: dict[str, dict] = {}  # path -> {last_accessed, access_count, created}


def _load_article_meta():
    global _article_meta
    if ARTICLE_META_PATH.exists():
        try:
            _article_meta = json.loads(ARTICLE_META_PATH.read_text(encoding="utf-8"))
        except Exception:
            _article_meta = {}


def _save_article_meta():
    ARTICLE_META_PATH.write_text(json.dumps(_article_meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _track_access(paths: list[str]):
    """Update access stats for given article paths."""
    now = datetime.now().isoformat()
    for path in paths:
        if path not in _article_meta:
            _article_meta[path] = {"last_accessed": now, "access_count": 0, "created": now}
        _article_meta[path]["last_accessed"] = now
        _article_meta[path]["access_count"] = _article_meta[path].get("access_count", 0) + 1
    _save_article_meta()


def _decay_factor(path: str) -> float:
    """Calculate temporal decay factor (0.3 - 1.0). Recent = higher score."""
    meta = _article_meta.get(path)
    if not meta or "last_accessed" not in meta:
        return 0.7  # neutral for unknown
    try:
        last = datetime.fromisoformat(meta["last_accessed"])
        days = (datetime.now() - last).days
        return max(0.3, 1.0 / (1.0 + days / 30.0))
    except Exception:
        return 0.7

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


# ─── Whoosh индекс ───────────────────────────────────────────────────────────

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

    # 3. Merge: 0.4 * BM25 + 0.6 * semantic
    all_paths = set(bm25_scores.keys()) | set(sem_scores.keys())
    merged = []
    for path in all_paths:
        bm25_norm = bm25_scores[path]["bm25"] if path in bm25_scores else 0
        sem_norm = sem_scores.get(path, 0)
        combined = 0.4 * bm25_norm + 0.6 * sem_norm

        if path in bm25_scores:
            info = bm25_scores[path]
        else:
            # Semantic-only result — get info from embed_texts
            proj = path.split("/", 1)[0] if "/" in path else "unknown"
            fname = path.split("/", 1)[-1] if "/" in path else path
            title = _embed_texts.get(path, fname)
            # Read preview from file
            fpath = KNOWLEDGE_DIR / path
            preview = ""
            if fpath.exists():
                lines = fpath.read_text(encoding="utf-8").splitlines()[:20]
                preview = "\n".join(lines)
            info = {"title": title, "project": proj, "file": fname, "preview": preview}

        # Apply temporal decay
        decay = _decay_factor(path)
        combined = combined * (0.7 + 0.3 * decay)  # 70% base + 30% recency bonus
        info["score"] = round(combined * 100, 1)
        merged.append(info)

    merged.sort(key=lambda x: x["score"], reverse=True)
    # Remove internal fields, filter low relevance
    for m in merged:
        m.pop("bm25", None)
    # Keep only results with meaningful relevance
    if merged:
        top_score = merged[0]["score"]
        threshold = max(top_score * 0.4, 25)  # min 25% absolute score
        merged = [m for m in merged if m["score"] >= threshold]
    return merged[:limit]


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def today_log_path() -> Path:
    d = date.today().isoformat()
    p = KNOWLEDGE_DIR / "daily" / f"{d}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def project_dir(project: str) -> Path:
    global PROJECTS
    p = KNOWLEDGE_DIR / project
    p.mkdir(parents=True, exist_ok=True)
    if project not in PROJECTS:
        PROJECTS = _discover_projects()
    return p


def _find_existing_article(topic: str, content: str, project: str) -> Optional[Path]:
    """Find existing article by semantic similarity or slug match."""
    proj_path = project_dir(project)
    if not proj_path.exists():
        return None

    slug = re.sub(r"[^\w\-]", "_", topic.lower())[:50]
    articles = list(proj_path.glob("*.md"))
    if not articles:
        return None

    # 1. Exact slug match (strip date prefix from old-format filenames)
    for a in articles:
        stem = a.stem
        clean_stem = re.sub(r"^\d{8}_", "", stem)  # remove YYYYMMDD_ prefix
        if clean_stem == slug:
            return a

    # 2. Semantic similarity match
    if not _embeddings:
        return None
    model = get_embed_model()
    query_text = f"{topic} {content[:300]}"
    q_vec = model.encode([query_text], normalize_embeddings=True)[0]

    best_path = None
    best_sim = 0.0
    for key, vec in _embeddings.items():
        if not key.startswith(f"{project}/") or key.startswith("daily/"):
            continue
        sim = float(np.dot(q_vec, vec))
        if sim > best_sim:
            best_sim = sim
            best_path = key

    if best_sim >= 0.75 and best_path:
        candidate = KNOWLEDGE_DIR / best_path
        if candidate.exists():
            return candidate

    return None


def _merge_into_article(article_path: Path, new_content: str, new_tags: list[str], ts: str):
    """Merge new content into existing article, update tags and timestamp."""
    text = article_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Update tags — merge old and new
    new_tag_set = set(new_tags) if new_tags else set()
    updated_lines = []
    for line in lines:
        if line.startswith("**Теги:**"):
            old_tags_str = line.split(":", 1)[1].strip().strip("*").strip()
            old_tags = {t.strip().strip("*").strip() for t in old_tags_str.split(",") if t.strip().strip("*").strip() and t.strip() != "—"}
            merged_tags = sorted(old_tags | new_tag_set)
            updated_lines.append(f"**Теги:** {', '.join(merged_tags) if merged_tags else '—'}")
        elif line.startswith("**Обновлено:**"):
            updated_lines.append(f"**Обновлено:** {ts}")
        elif line.startswith("**Дата:**"):
            # Keep original date, add/update Обновлено after it
            updated_lines.append(line)
            # Check if next line is Обновлено — if not, insert it
            idx = lines.index(line)
            if idx + 1 < len(lines) and lines[idx + 1].startswith("**Обновлено:**"):
                pass  # will be updated in the loop
            else:
                updated_lines.append(f"**Обновлено:** {ts}")
        else:
            updated_lines.append(line)

    # Add new entry section
    updated_text = "\n".join(updated_lines)
    if "## Записи" not in updated_text:
        # First update of old-format article: wrap existing content
        # Find where body starts (after metadata)
        body_start = 0
        for i, line in enumerate(updated_lines):
            if line.startswith("**") and ":" in line:
                body_start = i + 1
            elif i > 0 and line.strip() == "" and body_start > 0:
                body_start = i + 1
                break
        # Extract existing body
        existing_body = "\n".join(updated_lines[body_start:]).strip()
        # Find original date
        orig_date = ts
        for line in updated_lines:
            if line.startswith("**Дата:**"):
                orig_date = line.split(":", 1)[1].strip()
                break
        # Rebuild with sections
        header = "\n".join(updated_lines[:body_start])
        updated_text = f"{header}\n\n## Записи\n\n### {orig_date}\n{existing_body}\n\n### {ts}\n{new_content}\n"
    else:
        updated_text += f"\n\n### {ts}\n{new_content}\n"

    article_path.write_text(updated_text, encoding="utf-8")


def _regenerate_index():
    """Auto-generate index.md from all project articles."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections = []
    total = 0

    for proj in PROJECTS:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        articles = sorted(proj_path.glob("*.md"))
        if not articles:
            continue
        items = []
        for a in articles:
            text = a.read_text(encoding="utf-8")
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else a.stem
            tags = ""
            for line in lines[:10]:
                if line.lower().startswith("**теги:**"):
                    tags = line.split(":", 1)[1].strip()
                    break
            items.append(f"- [{title}](./{proj}/{a.name}) — {tags}")
        total += len(articles)
        sections.append(f"### {proj} ({len(articles)} ст.)\n" + "\n".join(items))

    # Daily log stats
    daily_dir = KNOWLEDGE_DIR / "daily"
    daily_count = len(list(daily_dir.glob("*.md"))) if daily_dir.exists() else 0

    index_text = f"""# Knowledge Base Index

Автоматически обновлено: {now}

## Проекты

{chr(10).join(sections)}

## Статистика
- Всего статей: {total}
- Daily логов: {daily_count}
- Последнее обновление: {now}
"""
    index_path = KNOWLEDGE_DIR / "index.md"
    index_path.write_text(index_text, encoding="utf-8")


# ─── Git версионирование ─────────────────────────────────────────────────────

def _git_init():
    """Initialize git repo in knowledge dir if not exists."""
    git_dir = KNOWLEDGE_DIR / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        subprocess.run(["git", "config", "user.email", "memory-compiler@nas"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        subprocess.run(["git", "config", "user.name", "memory-compiler"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        # Gitignore for index/cache files
        gitignore = KNOWLEDGE_DIR / ".gitignore"
        gitignore.write_text(".whoosh_index/\n.embeddings.pkl\n", encoding="utf-8")
        _git_commit("init knowledge base")


def _git_commit(message: str):
    """Stage all and commit."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(KNOWLEDGE_DIR), capture_output=True
        )
        if result.returncode != 0:  # there are staged changes
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(KNOWLEDGE_DIR), capture_output=True
            )
    except Exception:
        pass  # git not available — silently skip


# ─── Инструменты ──────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="save_lesson",
            description="Сохранить или обновить статью в базе знаний. Автоматически находит существующую статью по теме и мержит новые факты.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Короткое название"},
                    "content": {"type": "string", "description": "Проблема, причина, решение"},
                    "project": {"type": "string", "description": "Имя проекта"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "force_new": {"type": "boolean", "default": False, "description": "Принудительно создать новую статью"}
                },
                "required": ["topic", "content", "project"]
            }
        ),
        Tool(
            name="get_context",
            description="Получить контекст из базы знаний перед началом нетривиальной задачи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "query": {"type": "string", "description": "Описание задачи"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="search",
            description="Найти похожие кейсы и решения в базе знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string", "default": "all", "description": "Имя проекта или 'all'"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="compile",
            description="Скомпилировать daily логи в проектные статьи. Мержит записи в существующие статьи или создаёт новые. dry_run=true для превью.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "default": True, "description": "Превью без изменений"},
                    "project": {"type": "string", "enum": PROJECTS + ["all"], "description": "Компилировать только записи этого проекта"},
                    "since": {"type": "string", "description": "ISO дата — обрабатывать логи начиная с этой даты"}
                }
            }
        ),
        Tool(
            name="lint",
            description="Проверить здоровье базы знаний: дубли, устаревшее, пустые статьи, теги.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "all", "description": "Имя проекта или 'all'"},
                    "fix": {"type": "boolean", "default": False, "description": "Автоисправление безопасных проблем (теги, index)"}
                }
            }
        ),
        Tool(
            name="reindex",
            description="Переиндексировать базу знаний (Whoosh BM25F + embeddings + index.md).",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="save_session",
            description="Сохранить контекст сессии (что сделано, что осталось, решения). Вызывать в конце сессии.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "summary": {"type": "string", "description": "Что сделано в этой сессии"},
                    "decisions": {"type": "string", "description": "Принятые решения"},
                    "open_questions": {"type": "string", "description": "Что осталось / открытые вопросы"}
                },
                "required": ["project", "summary"]
            }
        ),
        Tool(
            name="load_session",
            description="Загрузить контекст предыдущей сессии. Вызывать в начале сессии.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="get_summary",
            description="Получить сжатую сводку проекта (заголовки, теги, ключевые факты). ~200 токенов.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="ask",
            description="Задать вопрос — получить ответ с цитатами из статей базы знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Вопрос на естественном языке"},
                    "project": {"type": "string", "default": "all", "description": "Имя проекта или 'all'"}
                },
                "required": ["question"]
            }
        ),
        Tool(
            name="get_active_context",
            description="Получить активный контекст проекта — последние 10 действий/решений.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="delete_article",
            description="Удалить статью из базы знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "filename": {"type": "string", "description": "Имя файла статьи (например, my_article.md)"}
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="edit_article",
            description="Заменить содержимое статьи или добавить секцию.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "filename": {"type": "string", "description": "Имя файла статьи"},
                    "content": {"type": "string", "description": "Новое содержимое (полная замена тела статьи)"},
                    "append": {"type": "boolean", "default": False, "description": "True — дописать в конец, False — заменить тело"}
                },
                "required": ["project", "filename", "content"]
            }
        ),
        Tool(
            name="read_article",
            description="Получить полный текст статьи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта или 'daily'"},
                    "filename": {"type": "string", "description": "Имя файла статьи"}
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="search_by_tag",
            description="Найти все статьи с указанным тегом.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Тег для поиска"},
                    "project": {"type": "string", "default": "all", "description": "Имя проекта или 'all'"}
                },
                "required": ["tag"]
            }
        ),
        Tool(
            name="article_history",
            description="Получить историю изменений статьи (git log).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "filename": {"type": "string", "description": "Имя файла статьи"}
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="add_project",
            description="Создать новый проект в базе знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя проекта (латиница, без пробелов)"}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="remove_project",
            description="Удалить проект из базы знаний (все статьи проекта будут удалены).",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя проекта для удаления"}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="list_projects",
            description="Список всех проектов с количеством статей.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name in _stats:
        _stats[name] = _stats.get(name, 0) + 1
    if name == "save_lesson":
        result = await _save_lesson(**arguments)
    elif name == "get_context":
        result = await _get_context(**arguments)
    elif name == "search":
        result = await _search(**arguments)
    elif name == "compile":
        result = await _compile(arguments.get("dry_run", True), arguments.get("project"), arguments.get("since"))
    elif name == "lint":
        result = await _lint(arguments.get("project", "all"), arguments.get("fix", False))
    elif name == "reindex":
        count = rebuild_index()
        ecount = rebuild_embeddings()
        _regenerate_index()
        result = [TextContent(type="text", text=f"✅ Переиндексировано: {count} документов (BM25F + {ecount} embeddings), index.md обновлён")]
    elif name == "save_session":
        result = await _save_session(**arguments)
    elif name == "load_session":
        result = await _load_session(**arguments)
    elif name == "get_summary":
        result = await _get_summary(**arguments)
    elif name == "ask":
        result = await _ask(**arguments)
    elif name == "get_active_context":
        result = await _get_active_context(**arguments)
    elif name == "delete_article":
        result = await _delete_article(**arguments)
    elif name == "edit_article":
        result = await _edit_article(**arguments)
    elif name == "read_article":
        result = await _read_article(**arguments)
    elif name == "search_by_tag":
        result = await _search_by_tag(**arguments)
    elif name == "article_history":
        result = await _article_history(**arguments)
    elif name == "add_project":
        result = await _add_project(**arguments)
    elif name == "remove_project":
        result = await _remove_project(**arguments)
    elif name == "list_projects":
        result = await _list_projects()
    else:
        result = [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    # Track response size
    total = sum(len(t.text) for t in result)
    _stats["total_chars_returned"] = _stats.get("total_chars_returned", 0) + total
    return result


# ─── Реализация ───────────────────────────────────────────────────────────────

async def _save_lesson(topic: str, content: str, project: str, tags: list = None, force_new: bool = False) -> list[TextContent]:
    tags = tags or []
    # Автотегирование — дополнить пользовательские теги автоматическими
    auto = _auto_tags(content, topic)
    existing_lower = {t.lower() for t in tags}
    tags = tags + [t for t in auto if t not in existing_lower]
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    slug = re.sub(r"[^\w\-]", "_", topic.lower())[:50]

    # 1. Always append to daily log (audit trail)
    log_path = today_log_path()
    separator = "\n---\n" if log_path.exists() and log_path.stat().st_size > 0 else ""
    entry = f"""{separator}\n## {topic}\n\n**Время:** {ts}\n**Проект:** {project}\n**Теги:** {', '.join(tags) if tags else '—'}\n\n{content}\n"""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)

    # 2. Find existing article or create new
    existing = None if force_new else _find_existing_article(topic, content, project)

    if existing:
        # Update existing article
        _merge_into_article(existing, content, tags, ts)
        article_path = existing
        article_text = article_path.read_text(encoding="utf-8")
        action = f"🔄 Обновлено: {project}/{article_path.name}"
    else:
        # Create new article
        article_path = project_dir(project) / f"{slug}.md"
        # Handle name collision
        if article_path.exists():
            article_path = project_dir(project) / f"{slug}_{now.strftime('%Y%m%d')}.md"
        article_text = f"""# {topic}\n\n**Дата:** {ts}\n**Проект:** {project}\n**Теги:** {', '.join(tags) if tags else '—'}\n\n## Записи\n\n### {ts}\n{content}\n"""
        article_path.write_text(article_text, encoding="utf-8")
        _regenerate_index()
        action = f"✅ Создано: {project}/{article_path.name}"

    # 3. Git-линковка — извлечь и добавить git-ссылки
    git_refs = _extract_git_refs(content, topic)
    if git_refs:
        refs_text = _format_git_refs(git_refs)
        article_text = article_path.read_text(encoding="utf-8")
        if "## Git-ссылки" not in article_text:
            article_text = article_text.rstrip() + f"\n\n## Git-ссылки\n{refs_text}\n"
        else:
            # Обновить существующую секцию — дополнить новыми
            existing_end = article_text.index("## Git-ссылки") + len("## Git-ссылки")
            article_text = article_text[:existing_end] + f"\n{refs_text}\n"
        article_path.write_text(article_text, encoding="utf-8")

    # 4. Update search indexes
    article_text = article_path.read_text(encoding="utf-8")
    index_document(article_text, article_path.name, project)
    embed_document(article_text, article_path.name, project)

    # 6. Обнаружение противоречий
    saved_key = f"{project}/{article_path.name}"
    contradictions = _detect_contradictions(content, project, exclude_path=saved_key)

    # 7. Cross-references
    _update_cross_references(topic, project, saved_key)

    # 8. Active Context
    _update_active_context(project, topic, content)

    # 9. Track access
    _track_access([saved_key])

    # 10. Git commit
    _git_commit(f"save: {topic} [{project}]")

    result = action
    if git_refs:
        refs_summary = ", ".join(f"{k}: {', '.join(v)}" for k, v in git_refs.items())
        result += f"\n🔗 Git: {refs_summary}"
    if contradictions:
        result += "\n\n⚠️ Возможные противоречия:\n" + "\n".join(f"  - {c}" for c in contradictions)
    return [TextContent(type="text", text=result)]


async def _get_context(project: str, query: str = None) -> list[TextContent]:
    if query:
        # Search in target project + cross-project results
        results = whoosh_search(query, project=project, limit=3)
        cross = whoosh_search(query, project="all", limit=3) if project != "all" else []
        # Add cross-project results that aren't already in main results
        seen = {r["file"] for r in results}
        for r in cross:
            if r["file"] not in seen and r["project"] != project:
                results.append(r)
                if len(results) >= 5:
                    break
        if not results:
            return [TextContent(type="text", text=f"Ничего не найдено по '{query}' в {project}.")]
        out = [f"# Контекст: {project} (query: {query})\n"]
        for r in results:
            preview = "\n".join(r["preview"].splitlines()[:8])
            out.append(f"---\n### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")
        return [TextContent(type="text", text="\n".join(out))]
    else:
        proj_path = project_dir(project)
        articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not articles:
            return [TextContent(type="text", text=f"База знаний по '{project}' пуста.")]
        selected = [(a, a.read_text(encoding="utf-8")) for a in articles[:5]]
        out = [f"# Контекст: {project}\n"]
        for path, text in selected:
            preview = "\n".join(text.splitlines()[:8])
            out.append(f"---\n### {path.stem}\n{preview}\n")
        return [TextContent(type="text", text="\n".join(out))]


async def _search(query: str, project: str = "all") -> list[TextContent]:
    results = whoosh_search(query, project=project, limit=8)
    if not results:
        return [TextContent(type="text", text=f"Ничего не найдено: '{query}'")]

    # Track access
    _track_access([f"{r['project']}/{r['file']}" for r in results])

    out = [f"# Поиск: '{query}'\n"]
    for r in results:
        preview_lines = r["preview"].splitlines()[:10]
        out.append(f"---\n### [{r['project']}] {r['title']} (score: {r['score']})\n" + "\n".join(preview_lines) + "\n")

    return [TextContent(type="text", text="\n".join(out))]


def _parse_daily_entries(text: str) -> list[dict]:
    """Parse daily log into individual entries. Split only by --- separator."""
    entries = []
    # Split by --- separator only (not by ## headers which may be inside content)
    parts = re.split(r'\n---\n', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.splitlines()
        # Find the top-level ## title (first ## in the entry)
        title = ""
        title_idx = -1
        for i, line in enumerate(lines):
            if line.startswith("## "):
                title = line[3:].strip()
                title_idx = i
                break
        if not title:
            continue
        # Extract metadata from lines after title
        project = "general"
        tags = []
        ts = ""
        body_start = title_idx + 1
        for i in range(title_idx + 1, min(title_idx + 8, len(lines))):
            line = lines[i]
            if line.startswith("**Время:**") or line.startswith("**Дата:**"):
                ts = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()
                body_start = i + 1
            elif line.startswith("**Проект:**"):
                project = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()
                body_start = i + 1
            elif line.startswith("**Теги:**"):
                tags_str = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()
                tags = [t.strip() for t in tags_str.split(",") if t.strip() and t.strip() != "—"]
                body_start = i + 1
            elif line.strip() == "":
                body_start = i + 1
            elif not line.startswith("**"):
                break  # body started
        body = "\n".join(lines[body_start:]).strip()
        if body:
            entries.append({"topic": title, "project": project, "tags": tags, "timestamp": ts, "content": body})
    return entries


async def _compile(dry_run: bool = True, project: str = None, since: str = None) -> list[TextContent]:
    daily_dir = KNOWLEDGE_DIR / "daily"
    if not daily_dir.exists():
        return [TextContent(type="text", text="Дневных логов нет.")]
    logs = sorted(daily_dir.glob("*.md"))
    if not logs:
        return [TextContent(type="text", text="Дневных логов нет.")]

    # Filter by date
    if since:
        logs = [l for l in logs if l.stem >= since]

    out = []
    total_entries = 0
    updated = 0
    created = 0
    processed_logs = []

    for log in logs:
        text = log.read_text(encoding="utf-8")
        entries = _parse_daily_entries(text)
        if not entries:
            continue

        for entry in entries:
            # Filter by project
            if project and entry["project"] != project:
                continue
            total_entries += 1

            if entry["project"] not in PROJECTS:
                entry["project"] = "general"

            existing = _find_existing_article(entry["topic"], entry["content"], entry["project"])

            if dry_run:
                if existing:
                    out.append(f"  🔄 Мерж: «{entry['topic']}» → {existing.name}")
                else:
                    slug = re.sub(r'[^\w\-]', '_', entry['topic'].lower())[:50]
                    out.append(f"  ✅ Новая: «{entry['topic']}» → {entry['project']}/{slug}.md")
            else:
                ts = entry["timestamp"] or datetime.now().strftime("%Y-%m-%d %H:%M")
                if existing:
                    _merge_into_article(existing, entry["content"], entry["tags"], ts)
                    article_text = existing.read_text(encoding="utf-8")
                    index_document(article_text, existing.name, entry["project"])
                    embed_document(article_text, existing.name, entry["project"])
                    updated += 1
                else:
                    slug = re.sub(r'[^\w\-]', '_', entry['topic'].lower())[:50]
                    article_path = project_dir(entry["project"]) / f"{slug}.md"
                    if article_path.exists():
                        article_path = project_dir(entry["project"]) / f"{slug}_{datetime.now().strftime('%Y%m%d')}.md"
                    article_text = f"# {entry['topic']}\n\n**Дата:** {ts}\n**Проект:** {entry['project']}\n**Теги:** {', '.join(entry['tags']) if entry['tags'] else '—'}\n\n## Записи\n\n### {ts}\n{entry['content']}\n"
                    article_path.write_text(article_text, encoding="utf-8")
                    index_document(article_text, article_path.name, entry["project"])
                    embed_document(article_text, article_path.name, entry["project"])
                    created += 1

        processed_logs.append(log)

    if dry_run:
        header = f"# Compile preview — {total_entries} записей из {len(processed_logs)} логов\n"
        if not out:
            return [TextContent(type="text", text="Нечего компилировать.")]
        return [TextContent(type="text", text=header + "\n".join(out))]
    else:
        # Archive processed daily logs
        archive_dir = daily_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for log in processed_logs:
            log.rename(archive_dir / log.name)

        _regenerate_index()
        _git_commit(f"compile: {total_entries} entries, {updated} updated, {created} created")
        summary = f"✅ Скомпилировано: {total_entries} записей — {updated} обновлено, {created} создано, {len(processed_logs)} логов архивировано"
        return [TextContent(type="text", text=summary)]


async def _lint(project: str = "all", fix: bool = False) -> list[TextContent]:
    """Check knowledge base health."""
    issues = []
    fixed = []
    check_projects = PROJECTS if project == "all" else [project]

    for proj in check_projects:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        articles = list(proj_path.glob("*.md"))
        if not articles:
            continue

        for a in articles:
            text = a.read_text(encoding="utf-8")
            lines = text.splitlines()

            # Check 1: Empty or minimal
            body = "\n".join(lines[5:]).strip()  # skip header
            if len(body) < 50:
                issues.append(f"⚠️ [{proj}] {a.name} — пустая/минимальная статья ({len(body)} символов)")

            # Check 2: Missing metadata
            has_project = any(l.startswith("**Проект:**") for l in lines[:10])
            has_tags = any(l.startswith("**Теги:**") for l in lines[:10])
            has_date = any(l.startswith("**Дата:**") or l.startswith("**Обновлено:**") for l in lines[:10])
            if not has_project or not has_tags or not has_date:
                missing = []
                if not has_project: missing.append("Проект")
                if not has_tags: missing.append("Теги")
                if not has_date: missing.append("Дата")
                issues.append(f"⚠️ [{proj}] {a.name} — нет метаданных: {', '.join(missing)}")

            # Check 3: Stale (>90 days)
            updated = None
            for line in lines[:10]:
                if line.startswith("**Обновлено:**") or line.startswith("**Дата:**"):
                    date_str = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()[:10]
                    try:
                        updated = datetime.strptime(date_str, "%Y-%m-%d")
                    except ValueError:
                        pass
                    break
            if updated and (datetime.now() - updated).days > 90:
                days = (datetime.now() - updated).days
                issues.append(f"ℹ️ [{proj}] {a.name} — устарела ({days} дней без обновления)")

            # Check 4: Tag normalization
            for line in lines[:10]:
                if line.startswith("**Теги:**"):
                    tags_str = line.split(":", 1)[1].strip()
                    raw_tags = [t.strip() for t in tags_str.split(",") if t.strip() and t.strip() != "—"]
                    lower_tags = [t.lower() for t in raw_tags]
                    if raw_tags != lower_tags and raw_tags:
                        if fix:
                            new_line = f"**Теги:** {', '.join(lower_tags)}"
                            text = text.replace(line, new_line)
                            a.write_text(text, encoding="utf-8")
                            fixed.append(f"🔧 [{proj}] {a.name} — теги нормализованы")
                        else:
                            issues.append(f"ℹ️ [{proj}] {a.name} — теги с разным регистром: {', '.join(raw_tags)}")
                    break

        # Check 5: Duplicates (semantic similarity) — compare parent articles only
        proj_embeddings = {k: v for k, v in _embeddings.items()
                          if k.startswith(f"{proj}/") and "#chunk" not in k}
        keys = list(proj_embeddings.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                sim = float(np.dot(proj_embeddings[keys[i]], proj_embeddings[keys[j]]))
                if sim > 0.85:
                    name_i = keys[i].split("/", 1)[-1]
                    name_j = keys[j].split("/", 1)[-1]
                    issues.append(f"⚠️ [{proj}] Возможный дубль (sim={sim:.2f}): {name_i} ↔ {name_j}")

            # Check 6: Stale rotation (>180 days → archive)
            if updated and (datetime.now() - updated).days > 180:
                days = (datetime.now() - updated).days
                if fix:
                    archive_dir = proj_path / "archive"
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    a.rename(archive_dir / a.name)
                    fixed.append(f"🔧 [{proj}] {a.name} → archive/ ({days} дней)")
                else:
                    issues.append(f"⚠️ [{proj}] {a.name} — кандидат на архивацию ({days} дней)")

        # Check 7: Cross-references — find related articles
        if len(keys) >= 2:
            for key in keys:
                name = key.split("/", 1)[-1]
                related = []
                for other_key in keys:
                    if other_key == key:
                        continue
                    if key in proj_embeddings and other_key in proj_embeddings:
                        sim = float(np.dot(proj_embeddings[key], proj_embeddings[other_key]))
                        if 0.6 < sim < 0.85:  # related but not duplicate
                            other_name = other_key.split("/", 1)[-1]
                            related.append(other_name)
                if related:
                    issues.append(f"ℹ️ [{proj}] {name} — связанные: {', '.join(related[:3])}")

    if fix:
        _regenerate_index()
        fixed.append("🔧 index.md перегенерирован")

    out = [f"# Lint — проверка базы знаний\n"]
    if issues:
        out.append(f"## Проблемы ({len(issues)})\n")
        out.extend(issues)
    if fixed:
        out.append(f"\n## Исправлено ({len(fixed)})\n")
        out.extend(fixed)
    if not issues and not fixed:
        out.append("✅ Проблем не найдено")
    return [TextContent(type="text", text="\n".join(out))]


# ─── Session Handoff ─────────────────────────────────────────────────────────

async def _save_session(project: str, summary: str, decisions: str = "", open_questions: str = "") -> list[TextContent]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    session_path = project_dir(project) / "_session.md"
    text = f"""# Сессия: {project}

**Дата:** {now}

## Что сделано
{summary}

## Решения
{decisions or '—'}

## Открытые вопросы
{open_questions or '—'}
"""
    session_path.write_text(text, encoding="utf-8")
    _git_commit(f"session: {project}")
    return [TextContent(type="text", text=f"✅ Контекст сессии сохранён: {project}/_session.md")]


async def _load_session(project: str) -> list[TextContent]:
    session_path = project_dir(project) / "_session.md"
    parts = []
    if session_path.exists():
        parts.append(session_path.read_text(encoding="utf-8"))
    else:
        parts.append(f"Нет сохранённой сессии для {project}.")

    # Уведомления о stale статьях
    proj_path = KNOWLEDGE_DIR / project
    stale_count = 0
    if proj_path.exists():
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            text = md.read_text(encoding="utf-8")
            for line in text.splitlines()[:10]:
                if line.startswith("**Обновлено:**") or line.startswith("**Дата:**"):
                    date_str = line.split(":", 1)[1].strip().lstrip("*").rstrip("*").strip()[:10]
                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        if (datetime.now() - dt).days > 90:
                            stale_count += 1
                    except ValueError:
                        pass
                    break
    if stale_count > 0:
        parts.append(f"\n⚠️ {stale_count} статей в {project} не обновлялись >90 дней. Запусти `lint` для деталей.")

    return [TextContent(type="text", text="\n".join(parts))]


# ─── Сжатый индекс проекта ──────────────────────────────────────────────────

async def _get_summary(project: str) -> list[TextContent]:
    proj_path = project_dir(project)
    articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Исключаем служебные файлы
    articles = [a for a in articles if not a.name.startswith("_")]
    if not articles:
        return [TextContent(type="text", text=f"Проект {project} пуст.")]

    lines = [f"# {project} — сводка ({len(articles)} статей)\n"]
    for a in articles[:20]:
        text = a.read_text(encoding="utf-8")
        file_lines = text.splitlines()
        title = file_lines[0].lstrip("# ").strip() if file_lines else a.stem
        tags = ""
        for fl in file_lines[:10]:
            if fl.lower().startswith("**теги:**"):
                tags = fl.split(":", 1)[1].strip()
                break
        # Первые 2 строки тела (после метаданных)
        body_lines = []
        body_started = False
        for fl in file_lines:
            if fl.startswith("## Записи") or fl.startswith("### "):
                body_started = True
                continue
            if body_started and fl.strip() and not fl.startswith("**"):
                body_lines.append(fl.strip())
                if len(body_lines) >= 2:
                    break
        brief = " ".join(body_lines)[:120]
        lines.append(f"- **{title}** ({tags}) — {brief}")

    return [TextContent(type="text", text="\n".join(lines))]


# ─── Q&A tool ────────────────────────────────────────────────────────────────

async def _ask(question: str, project: str = "all") -> list[TextContent]:
    results = whoosh_search(question, project=project, limit=5)
    if not results:
        return [TextContent(type="text", text=f"Не найдено информации по: '{question}'")]

    _track_access([f"{r['project']}/{r['file']}" for r in results])

    out = [f"# Ответ на: {question}\n"]
    for r in results:
        # Читаем полный текст статьи для извлечения релевантных фрагментов
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        # Ищем параграфы, содержащие слова из вопроса
        q_words = set(w.lower() for w in question.split() if len(w) > 2)
        sections = text.split("\n### ")
        best_sections = []
        for sec in sections:
            sec_lower = sec.lower()
            matches = sum(1 for w in q_words if w in sec_lower)
            if matches > 0:
                best_sections.append((matches, sec.strip()))
        best_sections.sort(key=lambda x: x[0], reverse=True)

        if best_sections:
            # Берём лучший фрагмент (до 300 символов)
            fragment = best_sections[0][1][:300].strip()
            out.append(f"---\n**[{r['project']}/{r['file']}]** (релевантность: {r['score']})\n> {fragment}\n")
        else:
            preview = "\n".join(r["preview"].splitlines()[:5])
            out.append(f"---\n**[{r['project']}/{r['file']}]** (релевантность: {r['score']})\n> {preview}\n")

    return [TextContent(type="text", text="\n".join(out))]


# ─── Active Context ──────────────────────────────────────────────────────────

def _update_active_context(project: str, topic: str, content: str):
    """Обновить файл активного контекста проекта (FIFO, 10 записей)."""
    ctx_path = project_dir(project) / "_active_context.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    brief = content.replace("\n", " ")[:150]
    new_entry = f"- [{now}] **{topic}** — {brief}"

    entries = []
    if ctx_path.exists():
        text = ctx_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("- ["):
                entries.append(line)

    entries.insert(0, new_entry)
    entries = entries[:10]  # FIFO

    ctx_text = f"# Активный контекст: {project}\n\nПоследние действия:\n" + "\n".join(entries) + "\n"
    ctx_path.write_text(ctx_text, encoding="utf-8")


async def _get_active_context(project: str) -> list[TextContent]:
    ctx_path = project_dir(project) / "_active_context.md"
    if not ctx_path.exists():
        return [TextContent(type="text", text=f"Нет активного контекста для {project}.")]
    text = ctx_path.read_text(encoding="utf-8")
    return [TextContent(type="text", text=text)]


# ─── Обнаружение противоречий ────────────────────────────────────────────────

_FACT_PATTERNS = [
    (r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', "IP"),
    (r'\bv?(\d+\.\d+\.\d+)\b', "версия"),
    (r'(https?://[^\s\)]+)', "URL"),
    (r':(\d{2,5})\b', "порт"),
]


def _detect_contradictions(new_content: str, project: str, exclude_path: Optional[str] = None) -> list[str]:
    """Найти возможные противоречия с существующими статьями."""
    warnings = []
    # Извлечь факты из нового контента
    new_facts: dict[str, set[str]] = {}
    for pattern, label in _FACT_PATTERNS:
        found = set(re.findall(pattern, new_content))
        if found:
            new_facts[label] = found

    if not new_facts:
        return []

    # Проверить против существующих статей проекта
    proj_path = project_dir(project)
    for md in proj_path.glob("*.md"):
        if md.name.startswith("_"):
            continue
        rel_path = f"{project}/{md.name}"
        if exclude_path and rel_path == exclude_path:
            continue
        text = md.read_text(encoding="utf-8")
        for pattern, label in _FACT_PATTERNS:
            existing = set(re.findall(pattern, text))
            new_vals = new_facts.get(label, set())
            if not new_vals or not existing:
                continue
            # Если и в старом и в новом есть факты одного типа, но разные
            overlap = new_vals & existing
            diff = new_vals - existing
            if diff and existing:
                # Есть новые значения которых нет в старой статье — возможное обновление
                for d in list(diff)[:2]:
                    for e in list(existing)[:2]:
                        if d != e:
                            warnings.append(f"В {md.name} {label}: {e}, а в новой записи: {d}")
                            break

    return warnings[:5]  # макс 5 предупреждений


# ─── CRUD статей ─────────────────────────────────────────────────────────────

async def _delete_article(project: str, filename: str) -> list[TextContent]:
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    fpath.unlink()
    # Remove from indexes
    key = f"{project}/{filename}"
    _embeddings.pop(key, None)
    # Remove chunks too
    chunk_keys = [k for k in _embeddings if k.startswith(key + "#")]
    for ck in chunk_keys:
        _embeddings.pop(ck, None)
    _embed_texts.pop(key, None)
    _article_meta.pop(key, None)
    _save_article_meta()
    rebuild_index()
    _regenerate_index()
    _git_commit(f"delete: {filename} [{project}]")
    return [TextContent(type="text", text=f"🗑️ Удалено: {project}/{filename}")]


async def _edit_article(project: str, filename: str, content: str, append: bool = False) -> list[TextContent]:
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    if append:
        text = fpath.read_text(encoding="utf-8")
        text = text.rstrip() + f"\n\n### {ts}\n{content}\n"
        fpath.write_text(text, encoding="utf-8")
    else:
        # Сохраняем заголовок и метаданные, заменяем тело
        old_text = fpath.read_text(encoding="utf-8")
        lines = old_text.splitlines()
        header_lines = []
        for line in lines:
            header_lines.append(line)
            if line.strip() == "" and len(header_lines) > 3:
                break
            if line.startswith("**Теги:**"):
                header_lines.append("")
                break
        # Обновляем дату
        header = "\n".join(header_lines)
        if "**Обновлено:**" in header:
            header = re.sub(r"\*\*Обновлено:\*\*.*", f"**Обновлено:** {ts}", header)
        else:
            header = header.rstrip() + f"\n**Обновлено:** {ts}\n"
        fpath.write_text(f"{header}\n{content}\n", encoding="utf-8")

    article_text = fpath.read_text(encoding="utf-8")
    index_document(article_text, filename, project)
    embed_document(article_text, filename, project)
    _git_commit(f"edit: {filename} [{project}]")
    return [TextContent(type="text", text=f"✏️ {'Дописано' if append else 'Обновлено'}: {project}/{filename}")]


async def _read_article(project: str, filename: str) -> list[TextContent]:
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    text = fpath.read_text(encoding="utf-8")
    key = f"{project}/{filename}"
    _track_access([key])
    return [TextContent(type="text", text=text)]


async def _search_by_tag(tag: str, project: str = "all") -> list[TextContent]:
    tag_lower = tag.lower().strip()
    results = []
    check_projects = PROJECTS if project == "all" else [project]
    for proj in check_projects:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            text = md.read_text(encoding="utf-8")
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else md.stem
            for line in lines[:10]:
                if line.lower().startswith("**теги:**"):
                    tags_str = line.split(":", 1)[1].strip()
                    article_tags = [t.strip().lower().strip("*").strip() for t in tags_str.split(",")]
                    if tag_lower in article_tags:
                        preview = "\n".join(lines[:8])
                        results.append({"title": title, "project": proj, "file": md.name, "preview": preview})
                    break
    if not results:
        return [TextContent(type="text", text=f"Статей с тегом '{tag}' не найдено.")]
    _track_access([f"{r['project']}/{r['file']}" for r in results])
    out = [f"# Тег: {tag} ({len(results)} статей)\n"]
    for r in results:
        out.append(f"---\n### [{r['project']}] {r['title']}\n{r['file']}\n")
    return [TextContent(type="text", text="\n".join(out))]


async def _article_history(project: str, filename: str) -> list[TextContent]:
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists():
        return [TextContent(type="text", text=f"Статья не найдена: {project}/{filename}")]
    rel_path = f"{project}/{filename}"
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20", "--", rel_path],
            cwd=str(KNOWLEDGE_DIR), capture_output=True, text=True
        )
        log = result.stdout.strip()
        if not log:
            return [TextContent(type="text", text=f"Нет git-истории для {rel_path}")]
        return [TextContent(type="text", text=f"# История: {rel_path}\n\n```\n{log}\n```")]
    except Exception as e:
        return [TextContent(type="text", text=f"Ошибка git: {e}")]


# ─── Управление проектами ─────────────────────────────────────────────────────

async def _add_project(name: str) -> list[TextContent]:
    global PROJECTS
    name = re.sub(r'[^\w\-]', '', name.lower().strip())
    if not name:
        return [TextContent(type="text", text="Некорректное имя проекта.")]
    proj_path = KNOWLEDGE_DIR / name
    if proj_path.exists():
        return [TextContent(type="text", text=f"Проект '{name}' уже существует.")]
    proj_path.mkdir(parents=True, exist_ok=True)
    PROJECTS = _discover_projects()
    _git_commit(f"add project: {name}")
    return [TextContent(type="text", text=f"✅ Проект '{name}' создан. Всего проектов: {len(PROJECTS)}")]


async def _remove_project(name: str) -> list[TextContent]:
    global PROJECTS
    name = name.strip()
    proj_path = KNOWLEDGE_DIR / name
    if not proj_path.exists():
        return [TextContent(type="text", text=f"Проект '{name}' не найден.")]
    # Посчитать статьи
    articles = list(proj_path.glob("*.md"))
    if articles:
        # Удалить все статьи из индексов
        for md in articles:
            key = f"{name}/{md.name}"
            _embeddings.pop(key, None)
            chunk_keys = [k for k in list(_embeddings.keys()) if k.startswith(key + "#")]
            for ck in chunk_keys:
                _embeddings.pop(ck, None)
            _embed_texts.pop(key, None)
            _article_meta.pop(key, None)
    # Удалить папку
    import shutil
    shutil.rmtree(str(proj_path))
    _save_article_meta()
    PROJECTS = _discover_projects()
    rebuild_index()
    _regenerate_index()
    _git_commit(f"remove project: {name} ({len(articles)} articles)")
    return [TextContent(type="text", text=f"🗑️ Проект '{name}' удалён ({len(articles)} статей). Осталось проектов: {len(PROJECTS)}")]


async def _list_projects() -> list[TextContent]:
    PROJECTS[:] = _discover_projects()
    lines = [f"# Проекты ({len(PROJECTS)})\n"]
    for proj in PROJECTS:
        proj_path = KNOWLEDGE_DIR / proj
        if proj_path.exists():
            articles = [f for f in proj_path.glob("*.md") if not f.name.startswith("_")]
            size = sum(f.stat().st_size for f in articles)
            lines.append(f"- **{proj}** — {len(articles)} статей, {round(size/1024, 1)} KB")
        else:
            lines.append(f"- **{proj}** — пуст")
    return [TextContent(type="text", text="\n".join(lines))]


# ─── Автотегирование ─────────────────────────────────────────────────────────

_AUTO_TAG_RULES = [
    (r'\b(?:docker|dockerfile|docker-compose|контейнер)\b', 'docker'),
    (r'\b(?:nginx|reverse.proxy|ssl|https)\b', 'nginx'),
    (r'\b(?:1[cсС]|1С|bsl|epf|обработк[аи]|конфигурац)\b', '1c'),
    (r'\b(?:postgres|postgresql|pgdump|pg_dump|миграци[яи]|alembic)\b', 'postgres'),
    (r'\b(?:ssh|paramiko|scp|sftp)\b', 'ssh'),
    (r'\b(?:react|typescript|tsx|vite|shadcn)\b', 'frontend'),
    (r'\b(?:fastapi|uvicorn|pydantic|sqlalchemy)\b', 'backend'),
    (r'\b(?:redis|celery|celery.beat)\b', 'redis'),
    (r'\b(?:mikrotik|firewall|фаервол|маршрут)\b', 'mikrotik'),
    (r'\b(?:nas|synology|dsm)\b', 'nas'),
    (r'\b(?:git|commit|merge|branch|rebase)\b', 'git'),
    (r'\b(?:mcp|claude|anthropic)\b', 'mcp'),
    (r'\b(?:деплой|deploy|прод|production)\b', 'deploy'),
    (r'\b(?:bug|баг|fix|исправлен|ошибк[аи])\b', 'bugfix'),
]


def _auto_tags(content: str, topic: str) -> list[str]:
    """Извлечь теги из контента автоматически."""
    text = f"{topic} {content}".lower()
    found = set()
    for pattern, tag in _AUTO_TAG_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            found.add(tag)
    return sorted(found)


# ─── Git-линковка ────────────────────────────────────────────────────────────

_GIT_REF_PATTERNS = [
    (r'(?:^|\s)([a-f0-9]{7,40})(?:\s|$|[,.\)])', "commit"),       # abc1234 or full SHA
    (r'(?:[\w.-]+/[\w.-]+)?#(\d+)', "issue"),                       # #123 or org/repo#123
    (r'\b(v\d+\.\d+(?:\.\d+)?)\b', "tag"),                          # v1.3.47
    (r'\b(?:branch|ветк[аи])\s+["\']?([a-zA-Z][\w/.-]+)', "branch"), # branch feature/xxx
]


def _extract_git_refs(content: str, topic: str) -> dict[str, list[str]]:
    """Извлечь упоминания git-объектов из контента."""
    text = f"{topic}\n{content}"
    refs: dict[str, set[str]] = {}
    for pattern, ref_type in _GIT_REF_PATTERNS:
        found = re.findall(pattern, text)
        if found:
            refs.setdefault(ref_type, set()).update(found)
    # Отфильтровать ложные срабатывания для коммитов (исключить даты и т.п.)
    if "commit" in refs:
        refs["commit"] = {c for c in refs["commit"] if not c.isdigit() and len(c) >= 7}
    return {k: sorted(v) for k, v in refs.items() if v}


def _format_git_refs(refs: dict[str, list[str]]) -> str:
    """Форматировать git-ссылки для вставки в статью."""
    if not refs:
        return ""
    parts = []
    labels = {"commit": "Коммиты", "issue": "Issues/PR", "tag": "Теги", "branch": "Ветки"}
    for ref_type, values in refs.items():
        label = labels.get(ref_type, ref_type)
        parts.append(f"**{label}:** {', '.join(values)}")
    return "\n".join(parts)


# ─── Инкрементальная интеграция (cross-references) ───────────────────────────

def _update_cross_references(topic: str, project: str, saved_path: str):
    """Добавить ссылки в связанные статьи."""
    if not _embeddings:
        return
    model = get_embed_model()
    q_vec = model.encode([topic], normalize_embeddings=True)[0]
    now = datetime.now().strftime("%Y-%m-%d")

    for key, vec in _embeddings.items():
        if key == saved_path or key.startswith("daily/") or "#chunk" in key:
            continue
        sim = float(np.dot(q_vec, vec))
        if sim < 0.55 or sim > 0.85:
            continue  # слишком далеко или слишком близко (дубль)

        fpath = KNOWLEDGE_DIR / key
        if not fpath.exists() or fpath.name.startswith("_"):
            continue

        text = fpath.read_text(encoding="utf-8")
        ref_line = f"- [{topic}](../{saved_path}) ({now})"

        if "## См. также" in text:
            # Не добавлять дубли
            if saved_path in text:
                continue
            text = text.rstrip() + f"\n{ref_line}\n"
        else:
            text = text.rstrip() + f"\n\n## См. также\n{ref_line}\n"

        fpath.write_text(text, encoding="utf-8")


# ─── Веб-интерфейс (мобильный доступ) ────────────────────────────────────────

WEB_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memory Compiler</title>
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--accent:#58a6ff;--green:#238636;--red:#da3633}
[data-theme=light]{--bg:#fff;--bg2:#f6f8fa;--bg3:#e1e4e8;--border:#d0d7de;--text:#24292f;--text2:#57606a;--accent:#0969da;--green:#1a7f37;--red:#cf222e}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);padding:12px;max-width:720px;margin:0 auto}
h1{font-size:1.3em;margin-bottom:12px;color:var(--accent)}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.theme-toggle{background:none;border:1px solid var(--border);border-radius:6px;padding:4px 8px;color:var(--text2);cursor:pointer;font-size:14px}
.search-box{display:flex;gap:8px;margin-bottom:12px}
.search-box input{flex:1;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:16px}
.search-box button{padding:10px 16px;border:none;border-radius:6px;background:var(--green);color:#fff;font-size:14px;cursor:pointer}
.search-box select{padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:14px}
.tags-bar{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tag-chip{padding:3px 10px;border-radius:12px;background:var(--bg3);color:var(--accent);font-size:12px;cursor:pointer;border:1px solid var(--border)}
.tag-chip.active{background:var(--accent);color:#fff}
.projects{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.projects a{padding:6px 12px;border-radius:16px;background:var(--bg3);color:var(--accent);text-decoration:none;font-size:13px;border:1px solid var(--border)}
.projects a.active{background:#1f6feb;color:#fff}
.breadcrumb{font-size:0.8em;color:var(--text2);margin-bottom:8px}
.breadcrumb a{color:var(--accent);text-decoration:none}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:10px;position:relative}
.card h3{font-size:0.95em;color:var(--accent);margin-bottom:6px}
.card .meta{font-size:0.8em;color:var(--text2);margin-bottom:8px}
.card .body{white-space:pre-wrap;font-size:0.85em;color:var(--text);line-height:1.5;max-height:200px;overflow-y:auto}
.card .body h1,.card .body h2,.card .body h3{color:var(--accent);margin:8px 0 4px}
.card .body strong{color:var(--text)}
.card .body code{background:var(--bg3);padding:1px 4px;border-radius:3px;font-size:0.9em}
.card.expanded .body{max-height:none}
.card .actions{display:flex;gap:8px;margin-top:6px;align-items:center}
.card .expand{color:var(--accent);font-size:0.8em;cursor:pointer}
.card .btn-del{color:var(--red);font-size:0.75em;cursor:pointer;border:none;background:none;padding:2px 6px}
.empty{color:var(--text2);text-align:center;padding:40px 0}
.tab-bar{display:flex;gap:0;margin-bottom:16px;border-bottom:1px solid var(--border);overflow-x:auto}
.tab-bar a{padding:8px 12px;color:var(--text2);text-decoration:none;font-size:13px;border-bottom:2px solid transparent;white-space:nowrap}
.tab-bar a.active{color:var(--accent);border-bottom-color:var(--accent)}
.form-group{margin-bottom:12px}
.form-group label{display:block;font-size:0.85em;color:var(--text2);margin-bottom:4px}
.form-group input,.form-group textarea,.form-group select{width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:14px;font-family:inherit}
.form-group textarea{min-height:120px;resize:vertical}
.btn-save{padding:10px 20px;border:none;border-radius:6px;background:var(--green);color:#fff;font-size:14px;cursor:pointer;width:100%}
.msg{padding:8px 12px;border-radius:6px;margin-bottom:12px;font-size:0.9em}
.msg.ok{background:#1a3a1a;color:#3fb950;border:1px solid var(--green)}
.msg.err{background:#3a1a1a;color:#f85149;border:1px solid var(--red)}
</style>
</head>
<body>
<div class="header">
<h1>Memory Compiler</h1>
<button class="theme-toggle" onclick="toggleTheme()">&#9728;/&#9790;</button>
</div>
<div class="tab-bar">
<a href="#" class="active" onclick="showTab('search');return false" id="tab-search">Поиск</a>
<a href="#" onclick="showTab('add');return false" id="tab-add">Добавить</a>
<a href="#" onclick="showTab('graph');return false" id="tab-graph">Граф</a>
<a href="#" onclick="showTab('compile');return false" id="tab-compile">Компиляция</a>
<a href="#" onclick="showTab('analytics');return false" id="tab-analytics">Аналитика</a>
</div>
<div id="view-search">
<div class="search-box">
<input id="q" type="search" placeholder="Поиск по базе знаний...">
<select id="q-project"><option value="">Все проекты</option></select>
<button onclick="doSearch()">Найти</button>
</div>
<div class="tags-bar" id="tags-bar"></div>
<div class="projects" id="projects"></div>
<div id="results"></div>
</div>
<div id="view-add" style="display:none">
<div id="save-msg"></div>
<div class="form-group"><label>Тема</label><input id="f-topic" placeholder="Краткое название"></div>
<div class="form-group"><label>Проект</label><select id="f-project"></select></div>
<div class="form-group"><label>Теги (через запятую)</label><input id="f-tags" placeholder="docker, nas, fix"></div>
<div class="form-group"><label>Содержание</label><textarea id="f-content" placeholder="Проблема, решение, ключевые факты..."></textarea></div>
<button class="btn-save" onclick="doSave()">Сохранить</button>
</div>
<div id="view-graph" style="display:none">
<div id="graph-container" style="width:100%;height:500px;border:1px solid #30363d;border-radius:8px;background:#0d1117;position:relative">
<canvas id="graph-canvas" style="width:100%;height:100%"></canvas>
</div>
<div id="graph-info" class="empty">Загрузка графа...</div>
</div>
<div id="view-compile" style="display:none">
<div id="compile-msg"></div>
<div id="compile-preview" class="card" style="display:none"><pre></pre></div>
<div style="display:flex;gap:8px;margin-top:12px">
<button class="btn-save" onclick="doCompilePreview()" style="background:#1f6feb">Превью</button>
<button class="btn-save" onclick="doCompileRun()" style="background:#238636">Применить</button>
</div>
</div>
<div id="view-analytics" style="display:none">
<div id="analytics-content"></div>
</div>
<script>
let PROJECTS=[];
fetch("/api/health").then(function(r){return r.json()}).then(function(d){PROJECTS=Object.keys(d.projects||{});renderProjects();loadTags();
$("f-project").innerHTML=PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");
$("q-project").innerHTML='<option value="">All</option>'+PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");});
const $=id=>document.getElementById(id);
let current=null;

function showTab(t){
  ["search","add","graph","compile","analytics"].forEach(v=>{
    $("view-"+v).style.display=v===t?"block":"none";
    $("tab-"+v).className=v===t?"active":"";
  });
  if(t==="graph")loadGraph();
  if(t==="analytics")loadAnalytics();
}

function renderProjects(){
  $("projects").innerHTML=PROJECTS.map(p=>
    `<a href="#" data-p="${p}" class="${p===current?'active':''}" onclick="loadProject('${p}');return false">${p}</a>`
  ).join("");
}

async function doSearch(){
  const q=$("q").value.trim();
  if(!q)return;
  current=null;renderProjects();
  const r=await fetch("/api/search?q="+encodeURIComponent(q));
  const d=await r.json();
  renderResults(d.results);
}

async function loadProject(p){
  current=p;renderProjects();$("q").value="";
  const r=await fetch("/api/projects/"+p);
  const d=await r.json();
  renderResults(d.articles);
}

async function expandCard(proj,file,el){
  const card=el.closest(".card");
  if(card.classList.contains("expanded")){card.classList.remove("expanded");el.textContent="Развернуть";return;}
  const r=await fetch("/api/article/"+proj+"/"+file);
  const d=await r.json();
  card.querySelector(".body").innerHTML=md2html(d.content||"Ошибка загрузки");
  card.classList.add("expanded");
  el.textContent="Свернуть";
}
async function deleteArticle(proj,file,el){
  if(!confirm("Удалить "+proj+"/"+file+"?"))return;
  const r=await fetch("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({project:proj,filename:file})});
  const d=await r.json();
  if(d.result){el.closest(".card").remove();}
  else{alert(d.error||"Ошибка удаления");}
}

function md2html(s){
  return esc(s).replace(/^### (.+)$/gm,'<h3>$1</h3>')
    .replace(/^## (.+)$/gm,'<h2>$1</h2>')
    .replace(/^# (.+)$/gm,'<h1>$1</h1>')
    .replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/^- (.+)$/gm,'&bull; $1');
}
function renderResults(items){
  if(!items||!items.length){$("results").innerHTML='<div class="empty">Ничего не найдено</div>';return;}
  $("results").innerHTML=items.map(i=>{
    const bc=`<div class="breadcrumb"><a href="#" onclick="loadProject('${esc(i.project)}');return false">${esc(i.project)}</a> &rsaquo; ${esc(i.file)}</div>`;
    return `<div class="card">${bc}<h3>${esc(i.title)}</h3><div class="meta">${esc(i.project||"")} &middot; ${esc(i.file)}${i.score?' &middot; score: '+i.score:''}</div><div class="body">${md2html(i.preview)}</div><div class="actions"><span class="expand" onclick="expandCard('${esc(i.project)}','${esc(i.file)}',this)">Развернуть</span><button class="btn-del" onclick="deleteArticle('${esc(i.project)}','${esc(i.file)}',this)">Удалить</button></div></div>`;
  }).join("");
}

async function doSave(){
  const topic=$("f-topic").value.trim();
  const content=$("f-content").value.trim();
  const project=$("f-project").value;
  const tags=$("f-tags").value.trim();
  if(!topic||!content){$("save-msg").innerHTML='<div class="msg err">Заполните тему и содержание</div>';return;}
  const r=await fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic,content,project,tags})});
  const d=await r.json();
  if(d.result){$("save-msg").innerHTML=`<div class="msg ok">${esc(d.result)}</div>`;$("f-topic").value="";$("f-content").value="";$("f-tags").value="";}
  else{$("save-msg").innerHTML=`<div class="msg err">${esc(d.error||"Ошибка")}</div>`;}
}

function esc(s){return s?s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"):""}

// Theme toggle
function toggleTheme(){
  const cur=document.documentElement.getAttribute("data-theme");
  const next=cur==="light"?"dark":"light";
  document.documentElement.setAttribute("data-theme",next==="dark"?"":"light");
  localStorage.setItem("theme",next);
}
(function(){const t=localStorage.getItem("theme");if(t==="light")document.documentElement.setAttribute("data-theme","light");})();

// Tags bar
async function loadTags(){
  const r=await fetch("/api/tags");
  const d=await r.json();
  $("tags-bar").innerHTML=d.tags.slice(0,20).map(t=>
    `<span class="tag-chip" onclick="searchByTag('${esc(t.tag)}')">${esc(t.tag)} (${t.count})</span>`
  ).join("");
}
function searchByTag(tag){$("q").value=tag;doSearch();}

// Graph visualization with interaction
let graphData=null,graphNodes=[],graphNmap={};
async function loadGraph(){
  $("graph-info").textContent="Загрузка...";
  const r=await fetch("/api/graph");
  graphData=await r.json();
  $("graph-info").textContent=`${graphData.nodes.length} статей, ${graphData.edges.length} связей. Клик по узлу — открыть статью.`;
  setupGraph();
}
function setupGraph(){
  if(!graphData)return;
  const c=$("graph-canvas");
  const W=c.parentElement.clientWidth;
  const H=Math.max(400,Math.min(600,window.innerHeight-200));
  c.width=W;c.height=H;
  graphNodes=graphData.nodes.map((n,i)=>({...n,x:W/2+Math.cos(i*2.39)*W*0.35,y:H/2+Math.sin(i*2.39)*H*0.35,vx:0,vy:0}));
  graphNmap={};graphNodes.forEach(n=>graphNmap[n.id]=n);
  for(let iter=0;iter<80;iter++){
    graphNodes.forEach(a=>{graphNodes.forEach(b=>{
      if(a===b)return;
      let dx=a.x-b.x,dy=a.y-b.y,d=Math.sqrt(dx*dx+dy*dy)||1;
      if(d<80){let f=(80-d)*0.04;a.vx+=dx/d*f;a.vy+=dy/d*f;}
    });});
    graphData.edges.forEach(e=>{
      const s=graphNmap[e.source],t=graphNmap[e.target];
      if(!s||!t)return;
      let dx=t.x-s.x,dy=t.y-s.y,d=Math.sqrt(dx*dx+dy*dy)||1;
      let f=(d-120)*0.008*e.weight;
      s.vx+=dx/d*f;s.vy+=dy/d*f;t.vx-=dx/d*f;t.vy-=dy/d*f;
    });
    graphNodes.forEach(n=>{n.x+=n.vx*0.5;n.y+=n.vy*0.5;n.vx*=0.8;n.vy*=0.8;
      n.x=Math.max(40,Math.min(W-40,n.x));n.y=Math.max(40,Math.min(H-40,n.y));});
  }
  renderGraph();
  // Click handler
  c.onclick=function(ev){
    const rect=c.getBoundingClientRect();
    const mx=ev.clientX-rect.left,my=ev.clientY-rect.top;
    for(const n of graphNodes){
      const r=Math.max(5,Math.min(12,4+n.access_count));
      if(Math.hypot(n.x-mx,n.y-my)<r+5){
        const [proj,file]=n.id.split("/",2);
        showTab("search");loadProject(proj);
        return;
      }
    }
  };
  // Hover handler
  c.onmousemove=function(ev){
    const rect=c.getBoundingClientRect();
    const mx=ev.clientX-rect.left,my=ev.clientY-rect.top;
    let found=false;
    for(const n of graphNodes){
      const r=Math.max(5,Math.min(12,4+n.access_count));
      if(Math.hypot(n.x-mx,n.y-my)<r+5){
        c.style.cursor="pointer";
        c.title=n.title+" ("+n.project+")";
        found=true;break;
      }
    }
    if(!found){c.style.cursor="default";c.title="";}
  };
}
function renderGraph(){
  if(!graphData)return;
  const c=$("graph-canvas");
  const ctx=c.getContext("2d");
  ctx.clearRect(0,0,c.width,c.height);
  ctx.globalAlpha=0.25;
  graphData.edges.forEach(e=>{
    const s=graphNmap[e.source],t=graphNmap[e.target];
    if(!s||!t)return;
    ctx.strokeStyle=getComputedStyle(document.body).getPropertyValue("color")||"#30363d";
    ctx.lineWidth=Math.max(1,e.weight*2.5);
    ctx.beginPath();ctx.moveTo(s.x,s.y);ctx.lineTo(t.x,t.y);ctx.stroke();
  });
  ctx.globalAlpha=1;
  graphNodes.forEach(n=>{
    const r=Math.max(5,Math.min(12,4+n.access_count));
    ctx.fillStyle=n.color;ctx.beginPath();ctx.arc(n.x,n.y,r,0,Math.PI*2);ctx.fill();
    ctx.strokeStyle="rgba(255,255,255,0.3)";ctx.lineWidth=1;ctx.stroke();
    ctx.fillStyle=getComputedStyle(document.body).color||"#c9d1d9";
    ctx.font="10px -apple-system,sans-serif";ctx.textAlign="center";
    ctx.fillText(n.title.substring(0,25),n.x,n.y-r-5);
  });
}

// Compile
async function doCompilePreview(){
  $("compile-msg").innerHTML='<div class="msg ok">Загрузка...</div>';
  const r=await fetch("/api/compile/preview");
  const d=await r.json();
  $("compile-preview").style.display="block";
  $("compile-preview").querySelector("pre").textContent=d.preview;
  $("compile-msg").innerHTML="";
}
async function doCompileRun(){
  if(!confirm("Применить компиляцию?"))return;
  $("compile-msg").innerHTML='<div class="msg ok">Компиляция...</div>';
  const r=await fetch("/api/compile/run",{method:"POST"});
  const d=await r.json();
  $("compile-msg").innerHTML=`<div class="msg ok">${esc(d.result)}</div>`;
  $("compile-preview").style.display="none";
}

// Analytics
async function loadAnalytics(){
  $("analytics-content").innerHTML='<div class="empty">Загрузка...</div>';
  const r=await fetch("/api/analytics");
  const d=await r.json();
  let h=`<div class="card"><h3>Статистика</h3><pre>Всего статей: ${d.total_articles}\nОтслеживается: ${d.total_tracked}\nНикогда не открывались: ${d.never_accessed.length}</pre></div>`;
  if(d.top_accessed.length){
    h+=`<div class="card"><h3>Топ по обращениям</h3>`;
    d.top_accessed.forEach(i=>{
      h+=`<div style="padding:4px 0;border-bottom:1px solid #21262d"><span style="color:#58a6ff">${esc(i.title)}</span> <span style="color:#8b949e">${i.project} &middot; ${i.access_count} обр.</span></div>`;
    });
    h+=`</div>`;
  }
  if(d.never_accessed.length){
    h+=`<div class="card"><h3>Никогда не открывались</h3><pre>${d.never_accessed.join("\\n")}</pre></div>`;
  }
  $("analytics-content").innerHTML=h;
}

$("q").addEventListener("keydown",e=>{if(e.key==="Enter")doSearch()});
// projects loaded dynamically from /api/health
</script>
</body>
</html>"""


async def web_index(request: Request):
    return HTMLResponse(WEB_HTML)


async def web_search(request: Request):
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"results": []})
    results = whoosh_search(q, limit=15)
    return JSONResponse({"results": results})


async def web_project(request: Request):
    project = request.path_params["project"]
    proj_path = KNOWLEDGE_DIR / project
    if not proj_path.exists():
        return JSONResponse({"articles": []})
    articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for a in articles[:30]:
        text = a.read_text(encoding="utf-8")
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() if lines else a.stem
        preview = "\n".join(lines[:10])
        items.append({"title": title, "project": project, "file": a.name, "preview": preview})
    return JSONResponse({"articles": items})


# ─── SSE сервер ───────────────────────────────────────────────────────────────

async def web_article(request: Request):
    """Get full article text."""
    project = request.path_params["project"]
    filename = request.path_params["filename"]
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists() or not fpath.suffix == ".md":
        return JSONResponse({"error": "not found"}, status_code=404)
    text = fpath.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else filename
    return JSONResponse({"title": title, "project": project, "file": filename, "content": text})


async def web_save(request: Request):
    """Save a new lesson via web form."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    topic = data.get("topic", "").strip()
    content = data.get("content", "").strip()
    project = data.get("project", "general").strip()
    tags = data.get("tags", [])
    if not topic or not content:
        return JSONResponse({"error": "topic and content required"}, status_code=400)
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    result = await _save_lesson(topic, content, project, tags)
    return JSONResponse({"result": result[0].text})


async def web_health(request: Request):
    global PROJECTS
    PROJECTS = _discover_projects()
    ix = get_index()
    total_chars = 0
    total_articles = 0
    project_stats = {}
    for proj in PROJECTS:
        p = KNOWLEDGE_DIR / proj
        if not p.exists():
            continue
        articles = list(p.glob("*.md"))
        chars = sum(a.stat().st_size for a in articles)
        project_stats[proj] = {"articles": len(articles), "size_kb": round(chars / 1024, 1)}
        total_chars += chars
        total_articles += len(articles)
    daily_dir = KNOWLEDGE_DIR / "daily"
    daily_count = len(list(daily_dir.glob("*.md"))) if daily_dir.exists() else 0
    return JSONResponse({
        "status": "ok",
        "documents": ix.doc_count(),
        "embeddings": len(_embeddings),
        "total_articles": total_articles,
        "total_size_kb": round(total_chars / 1024, 1),
        "daily_logs": daily_count,
        "projects": project_stats,
        "usage": _stats,
    })


async def web_graph(request: Request):
    """Knowledge graph — nodes and edges from embeddings."""
    nodes = []
    edges = []
    # Collect parent-only embeddings
    parent_keys = [k for k in _embeddings.keys() if "#chunk" not in k and not k.split("/")[-1].startswith("_")]
    palette = ["#3B82F6", "#10B981", "#F59E0B", "#8B5CF6", "#EC4899", "#F97316", "#6B7280", "#EF4444", "#14B8A6", "#A855F7"]
    proj_colors = {p: palette[i % len(palette)] for i, p in enumerate(PROJECTS)}
    proj_colors["daily"] = "#9CA3AF"

    for key in parent_keys:
        proj = key.split("/")[0]
        title = _embed_texts.get(key, key.split("/")[-1])
        meta = _article_meta.get(key, {})
        nodes.append({
            "id": key, "title": title, "project": proj,
            "color": proj_colors.get(proj, "#6B7280"),
            "access_count": meta.get("access_count", 0),
        })

    # Build edges (similarity > 0.5)
    for i, k1 in enumerate(parent_keys):
        for k2 in parent_keys[i+1:]:
            sim = float(np.dot(_embeddings[k1], _embeddings[k2]))
            if sim > 0.5:
                edges.append({"source": k1, "target": k2, "weight": round(sim, 2)})

    return JSONResponse({"nodes": nodes, "edges": edges})


async def web_analytics(request: Request):
    """Analytics — article access stats."""
    _load_article_meta()
    items = []
    for path, meta in _article_meta.items():
        title = _embed_texts.get(path, path.split("/")[-1] if "/" in path else path)
        proj = path.split("/")[0] if "/" in path else "unknown"
        items.append({
            "path": path, "title": title, "project": proj,
            "access_count": meta.get("access_count", 0),
            "last_accessed": meta.get("last_accessed", ""),
        })
    items.sort(key=lambda x: x["access_count"], reverse=True)

    # Never accessed articles
    all_articles = set()
    for proj in PROJECTS:
        p = KNOWLEDGE_DIR / proj
        if p.exists():
            for md in p.glob("*.md"):
                if not md.name.startswith("_"):
                    all_articles.add(f"{proj}/{md.name}")
    never_accessed = all_articles - set(_article_meta.keys())

    return JSONResponse({
        "top_accessed": items[:20],
        "never_accessed": sorted(never_accessed)[:20],
        "total_tracked": len(_article_meta),
        "total_articles": len(all_articles),
    })


async def web_compile_preview(request: Request):
    """Preview what compile would do — with diffs."""
    result = await _compile(dry_run=True)
    return JSONResponse({"preview": result[0].text})


async def web_compile_run(request: Request):
    """Execute compile."""
    result = await _compile(dry_run=False)
    return JSONResponse({"result": result[0].text})


async def web_export(request: Request):
    """Export all articles from a project as JSON."""
    project = request.path_params["project"]
    proj_path = KNOWLEDGE_DIR / project
    if not proj_path.exists():
        return JSONResponse({"articles": []})
    articles = []
    for md in sorted(proj_path.glob("*.md")):
        if md.name.startswith("_"):
            continue
        text = md.read_text(encoding="utf-8")
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() if lines else md.stem
        articles.append({"filename": md.name, "title": title, "content": text})
    return JSONResponse({
        "project": project,
        "count": len(articles),
        "articles": articles,
    })


async def web_delete_article(request: Request):
    """Delete an article via web UI."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    project = data.get("project", "").strip()
    filename = data.get("filename", "").strip()
    if not project or not filename:
        return JSONResponse({"error": "project and filename required"}, status_code=400)
    result = await _delete_article(project, filename)
    return JSONResponse({"result": result[0].text})


async def web_tags(request: Request):
    """Get all tags with counts."""
    tag_counts: dict[str, int] = {}
    for proj in PROJECTS:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            text = md.read_text(encoding="utf-8")
            for line in text.splitlines()[:10]:
                if line.lower().startswith("**теги:**"):
                    tags_str = line.split(":", 1)[1].strip()
                    for t in tags_str.split(","):
                        t = t.strip().lower().strip("*").strip()
                        if t and t != "—":
                            tag_counts[t] = tag_counts.get(t, 0) + 1
                    break
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    return JSONResponse({"tags": [{"tag": t, "count": c} for t, c in sorted_tags]})


def create_starlette_app(mcp_server: Server) -> Starlette:
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )

    async def auto_compile_loop():
        """Run compile daily at 2 AM."""
        while True:
            now = datetime.now()
            # Next 2 AM
            target = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if target <= now:
                target = target.replace(day=target.day + 1)
            wait = (target - now).total_seconds()
            await asyncio.sleep(wait)
            try:
                result = await _compile(dry_run=False)
                print(f"Auto-compile: {result[0].text}")
            except Exception as e:
                print(f"Auto-compile error: {e}")

    @asynccontextmanager
    async def lifespan(app):
        _git_init()
        _load_article_meta()
        count = rebuild_index()
        print(f"Whoosh index built: {count} documents")
        if not load_embeddings() or len(_embeddings) != count:
            ecount = rebuild_embeddings()
            print(f"Embeddings built: {ecount} documents")
        else:
            print(f"Embeddings loaded from cache: {len(_embeddings)} documents")
        task = asyncio.create_task(auto_compile_loop())
        print("Auto-compile scheduled daily at 02:00")
        yield
        task.cancel()

    return Starlette(
        routes=[
            Route("/", endpoint=web_index),
            Route("/api/health", endpoint=web_health),
            Route("/api/search", endpoint=web_search),
            Route("/api/save", endpoint=web_save, methods=["POST"]),
            Route("/api/article/{project}/{filename}", endpoint=web_article),
            Route("/api/projects/{project}", endpoint=web_project),
            Route("/api/graph", endpoint=web_graph),
            Route("/api/analytics", endpoint=web_analytics),
            Route("/api/compile/preview", endpoint=web_compile_preview),
            Route("/api/compile/run", endpoint=web_compile_run, methods=["POST"]),
            Route("/api/export/{project}", endpoint=web_export),
            Route("/api/delete", endpoint=web_delete_article, methods=["POST"]),
            Route("/api/tags", endpoint=web_tags),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        lifespan=lifespan,
    )


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8765"))
    starlette_app = create_starlette_app(app)
    uvicorn.run(starlette_app, host=host, port=port)
