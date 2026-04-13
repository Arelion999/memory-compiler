# Memory-Compiler Refactor to Package

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor monolithic `server.py` (2480 lines) into a well-structured Python package `memory_compiler/` with pytest tests, updated Docker, and documentation.

**Architecture:** Split by responsibility into 7 modules: config, storage (articles/git/metadata), search (whoosh+embeddings), tools (MCP tool definitions and dispatch), handlers (tool implementations), api (REST endpoints), ui (HTML template). Entry point `server.py` stays as a thin launcher. Tests use pytest with a temporary knowledge directory.

**Tech Stack:** Python 3.12, MCP SDK, Starlette, Whoosh, sentence-transformers, pytest, Docker

---

### Task 1: Create Package Structure and Config Module

**Files:**
- Create: `memory_compiler/__init__.py`
- Create: `memory_compiler/config.py`
- Modify: `server.py` (will become thin launcher at Task 7)

- [ ] **Step 1: Create package directory and `__init__.py`**

```python
# memory_compiler/__init__.py
"""memory-compiler MCP server — knowledge base with hybrid search."""
```

- [ ] **Step 2: Extract config into `memory_compiler/config.py`**

Extract lines 1-67 of `server.py` — all imports, constants, KNOWLEDGE_DIR, PROJECTS, SCHEMA, analyzer, stats, article_meta globals:

```python
"""Configuration, constants, and shared state for memory-compiler."""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from whoosh.fields import Schema, TEXT, ID, STORED
from whoosh.analysis import RegexTokenizer, LowercaseFilter

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


PROJECTS = _discover_projects()

# Whoosh schema
analyzer = RegexTokenizer(r'[\w]{2,}') | LowercaseFilter()
SCHEMA = Schema(
    path=ID(stored=True, unique=True),
    project=ID(stored=True),
    title=TEXT(stored=True, analyzer=analyzer, field_boost=5.0),
    tags=TEXT(stored=True, analyzer=analyzer, field_boost=3.0),
    body=TEXT(analyzer=analyzer, field_boost=1.0),
    preview=STORED,
)

# Usage stats
stats = {"search": 0, "save": 0, "get_context": 0, "compile": 0, "lint": 0, "total_chars_returned": 0}

# Article metadata (temporal decay + analytics)
ARTICLE_META_PATH = KNOWLEDGE_DIR / ".article_meta.json"
article_meta: dict[str, dict] = {}


def load_article_meta():
    global article_meta
    if ARTICLE_META_PATH.exists():
        try:
            article_meta = json.loads(ARTICLE_META_PATH.read_text(encoding="utf-8"))
        except Exception:
            article_meta = {}


def save_article_meta():
    ARTICLE_META_PATH.write_text(json.dumps(article_meta, ensure_ascii=False, indent=2), encoding="utf-8")


def track_access(paths: list[str]):
    """Update access stats for given article paths."""
    now = datetime.now().isoformat()
    for path in paths:
        if path not in article_meta:
            article_meta[path] = {"last_accessed": now, "access_count": 0, "created": now}
        article_meta[path]["last_accessed"] = now
        article_meta[path]["access_count"] = article_meta[path].get("access_count", 0) + 1
    save_article_meta()


def decay_factor(path: str) -> float:
    """Calculate temporal decay factor (0.3 - 1.0). Recent = higher score."""
    meta = article_meta.get(path)
    if not meta or "last_accessed" not in meta:
        return 0.7
    try:
        last = datetime.fromisoformat(meta["last_accessed"])
        days = (datetime.now() - last).days
        return max(0.3, 1.0 / (1.0 + days / 30.0))
    except Exception:
        return 0.7
```

- [ ] **Step 3: Verify config module imports**

Run: `cd /path/to/memory-compiler && python -c "from memory_compiler.config import KNOWLEDGE_DIR, PROJECTS, SCHEMA; print('OK', len(PROJECTS))"`
Expected: `OK` with number of projects

- [ ] **Step 4: Commit**

```bash
git add memory_compiler/__init__.py memory_compiler/config.py
git commit -m "refactor: extract config module from server.py"
```

---

### Task 2: Extract Search Module

**Files:**
- Create: `memory_compiler/search.py`

- [ ] **Step 1: Create `memory_compiler/search.py`**

Extract lines 111-417 — all semantic search (embeddings) and Whoosh index code. Replace `_` prefixed globals with module-level variables. Import from config:

```python
"""Hybrid search: Whoosh BM25F + sentence-transformers semantic search."""
import pickle
from typing import Optional

import numpy as np
from sentence_transformers import SentenceTransformer
from whoosh import index as whoosh_index
from whoosh.qparser import MultifieldParser, OrGroup, AndGroup, FuzzyTermPlugin
from whoosh.scoring import BM25F

from memory_compiler.config import (
    KNOWLEDGE_DIR, INDEX_DIR, PROJECTS, SCHEMA,
    decay_factor, article_meta,
)

EMBEDDINGS_PATH = KNOWLEDGE_DIR / ".embeddings.pkl"
EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_embed_model: Optional[SentenceTransformer] = None
_embeddings: dict[str, np.ndarray] = {}
_embed_texts: dict[str, str] = {}
_ix = None  # global whoosh index


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
    """Split article into chunks by ### sections."""
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else ""
    tags = ""
    for line in lines[:10]:
        if line.lower().startswith("**теги:**"):
            tags = line.split(":", 1)[1].strip()
            break
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
    if len(sections) <= 1:
        return [(path_key, f"{title} {tags} {' '.join(lines[:30])[:500]}")]
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
    with open(EMBEDDINGS_PATH, "wb") as f:
        pickle.dump({"embeddings": _embeddings, "texts": _embed_texts}, f)


def semantic_search(query: str, limit: int = 10) -> list[tuple[str, float]]:
    """Search by semantic similarity. Deduplicates chunks to parent articles."""
    if not _embeddings:
        return []
    model = get_embed_model()
    q_vec = model.encode([query], normalize_embeddings=True)[0]
    raw_scores = []
    for path, vec in _embeddings.items():
        sim = float(np.dot(q_vec, vec))
        raw_scores.append((path, sim))
    raw_scores.sort(key=lambda x: x[1], reverse=True)
    seen = {}
    for path, sim in raw_scores:
        parent = path.split("#")[0]
        if parent not in seen or sim > seen[parent]:
            seen[parent] = sim
    results = sorted(seen.items(), key=lambda x: x[1], reverse=True)
    return results[:limit]


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
    parser.add_plugin(FuzzyTermPlugin())
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
                    "bm25": hit.score / max_bm25,
                }
    sem_scores: dict[str, float] = {}
    sem_results = semantic_search(query_str, limit=limit * 2)
    for path, sim in sem_results:
        if project != "all" and not path.startswith(project + "/"):
            continue
        sem_scores[path] = max(sim, 0)
    all_paths = set(bm25_scores.keys()) | set(sem_scores.keys())
    merged = []
    for path in all_paths:
        bm25_norm = bm25_scores[path]["bm25"] if path in bm25_scores else 0
        sem_norm = sem_scores.get(path, 0)
        combined = 0.4 * bm25_norm + 0.6 * sem_norm
        if path in bm25_scores:
            info = bm25_scores[path]
        else:
            proj = path.split("/", 1)[0] if "/" in path else "unknown"
            fname = path.split("/", 1)[-1] if "/" in path else path
            title = _embed_texts.get(path, fname)
            fpath = KNOWLEDGE_DIR / path
            preview = ""
            if fpath.exists():
                lines = fpath.read_text(encoding="utf-8").splitlines()[:20]
                preview = "\n".join(lines)
            info = {"title": title, "project": proj, "file": fname, "preview": preview}
        decay = decay_factor(path)
        combined = combined * (0.7 + 0.3 * decay)
        info["score"] = round(combined * 100, 1)
        merged.append(info)
    merged.sort(key=lambda x: x["score"], reverse=True)
    for m in merged:
        m.pop("bm25", None)
    if merged:
        top_score = merged[0]["score"]
        threshold = max(top_score * 0.4, 25)
        merged = [m for m in merged if m["score"] >= threshold]
    return merged[:limit]
```

- [ ] **Step 2: Verify search module imports**

Run: `cd /path/to/memory-compiler && python -c "from memory_compiler.search import whoosh_search, rebuild_index; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add memory_compiler/search.py
git commit -m "refactor: extract search module (whoosh + embeddings)"
```

---

### Task 3: Extract Storage Module

**Files:**
- Create: `memory_compiler/storage.py`

- [ ] **Step 1: Create `memory_compiler/storage.py`**

Extract lines 420-616 (utilities, article management, git) and lines 1440-1458 (active context), 1469-1517 (contradictions), 1751-1850 (auto-tagging, git-linking, cross-refs), 538-583 (regenerate index):

```python
"""Article storage, git versioning, utilities."""
import re
import subprocess
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import numpy as np

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, article_meta, save_article_meta,
    _discover_projects,
)
from memory_compiler.search import (
    get_embed_model, _embeddings, _embed_texts,
    rebuild_index, _regenerate_will_be_here,
)

# Forward reference — search module needs these, we need search module
# Resolved by importing at function level where needed


def today_log_path() -> Path:
    d = date.today().isoformat()
    p = KNOWLEDGE_DIR / "daily" / f"{d}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def project_dir(project: str) -> Path:
    from memory_compiler.config import PROJECTS, _discover_projects
    p = KNOWLEDGE_DIR / project
    p.mkdir(parents=True, exist_ok=True)
    if project not in PROJECTS:
        PROJECTS[:] = _discover_projects()
    return p


def find_existing_article(topic: str, content: str, project: str) -> Optional[Path]:
    """Find existing article by semantic similarity or slug match."""
    from memory_compiler.search import _embeddings, get_embed_model
    proj_path = project_dir(project)
    if not proj_path.exists():
        return None
    slug = re.sub(r"[^\w\-]", "_", topic.lower())[:50]
    articles = list(proj_path.glob("*.md"))
    if not articles:
        return None
    for a in articles:
        stem = a.stem
        clean_stem = re.sub(r"^\d{8}_", "", stem)
        if clean_stem == slug:
            return a
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


def merge_into_article(article_path: Path, new_content: str, new_tags: list[str], ts: str):
    """Merge new content into existing article, update tags and timestamp."""
    text = article_path.read_text(encoding="utf-8")
    lines = text.splitlines()
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
            updated_lines.append(line)
            idx = lines.index(line)
            if idx + 1 < len(lines) and lines[idx + 1].startswith("**Обновлено:**"):
                pass
            else:
                updated_lines.append(f"**Обновлено:** {ts}")
        else:
            updated_lines.append(line)
    updated_text = "\n".join(updated_lines)
    if "## Записи" not in updated_text:
        body_start = 0
        for i, line in enumerate(updated_lines):
            if line.startswith("**") and ":" in line:
                body_start = i + 1
            elif i > 0 and line.strip() == "" and body_start > 0:
                body_start = i + 1
                break
        existing_body = "\n".join(updated_lines[body_start:]).strip()
        orig_date = ts
        for line in updated_lines:
            if line.startswith("**Дата:**"):
                orig_date = line.split(":", 1)[1].strip()
                break
        header = "\n".join(updated_lines[:body_start])
        updated_text = f"{header}\n\n## Записи\n\n### {orig_date}\n{existing_body}\n\n### {ts}\n{new_content}\n"
    else:
        updated_text += f"\n\n### {ts}\n{new_content}\n"
    article_path.write_text(updated_text, encoding="utf-8")


def regenerate_index():
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


# --- Git ---

def git_init():
    """Initialize git repo in knowledge dir if not exists."""
    git_dir = KNOWLEDGE_DIR / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        subprocess.run(["git", "config", "user.email", "memory-compiler@nas"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        subprocess.run(["git", "config", "user.name", "memory-compiler"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        gitignore = KNOWLEDGE_DIR / ".gitignore"
        gitignore.write_text(".whoosh_index/\n.embeddings.pkl\n", encoding="utf-8")
        git_commit("init knowledge base")


def git_commit(message: str):
    """Stage all and commit."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(KNOWLEDGE_DIR), capture_output=True)
        if result.returncode != 0:
            subprocess.run(["git", "commit", "-m", message], cwd=str(KNOWLEDGE_DIR), capture_output=True)
    except Exception:
        pass


# --- Active Context ---

def update_active_context(project: str, topic: str, content: str):
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
    entries = entries[:10]
    ctx_text = f"# Активный контекст: {project}\n\nПоследние действия:\n" + "\n".join(entries) + "\n"
    ctx_path.write_text(ctx_text, encoding="utf-8")


# --- Contradictions ---

_FACT_PATTERNS = [
    (r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', "IP"),
    (r'\bv?(\d+\.\d+\.\d+)\b', "версия"),
    (r'(https?://[^\s\)]+)', "URL"),
    (r':(\d{2,5})\b', "порт"),
]


def detect_contradictions(new_content: str, project: str, exclude_path: Optional[str] = None) -> list[str]:
    """Найти возможные противоречия с существующими статьями."""
    warnings = []
    new_facts: dict[str, set[str]] = {}
    for pattern, label in _FACT_PATTERNS:
        found = set(re.findall(pattern, new_content))
        if found:
            new_facts[label] = found
    if not new_facts:
        return []
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
            diff = new_vals - existing
            if diff and existing:
                for d in list(diff)[:2]:
                    for e in list(existing)[:2]:
                        if d != e:
                            warnings.append(f"В {md.name} {label}: {e}, а в новой записи: {d}")
                            break
    return warnings[:5]


# --- Auto-tagging ---

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


def auto_tags(content: str, topic: str) -> list[str]:
    """Извлечь теги из контента автоматически."""
    text = f"{topic} {content}".lower()
    found = set()
    for pattern, tag in _AUTO_TAG_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            found.add(tag)
    return sorted(found)


# --- Git-linking ---

_GIT_REF_PATTERNS = [
    (r'(?:^|\s)([a-f0-9]{7,40})(?:\s|$|[,.\)])', "commit"),
    (r'(?:[\w.-]+/[\w.-]+)?#(\d+)', "issue"),
    (r'\b(v\d+\.\d+(?:\.\d+)?)\b', "tag"),
    (r'\b(?:branch|ветк[аи])\s+["\']?([a-zA-Z][\w/.-]+)', "branch"),
]


def extract_git_refs(content: str, topic: str) -> dict[str, list[str]]:
    """Извлечь упоминания git-объектов из контента."""
    text = f"{topic}\n{content}"
    refs: dict[str, set[str]] = {}
    for pattern, ref_type in _GIT_REF_PATTERNS:
        found = re.findall(pattern, text)
        if found:
            refs.setdefault(ref_type, set()).update(found)
    if "commit" in refs:
        refs["commit"] = {c for c in refs["commit"] if not c.isdigit() and len(c) >= 7}
    return {k: sorted(v) for k, v in refs.items() if v}


def format_git_refs(refs: dict[str, list[str]]) -> str:
    """Форматировать git-ссылки для вставки в статью."""
    if not refs:
        return ""
    parts = []
    labels = {"commit": "Коммиты", "issue": "Issues/PR", "tag": "Теги", "branch": "Ветки"}
    for ref_type, values in refs.items():
        label = labels.get(ref_type, ref_type)
        parts.append(f"**{label}:** {', '.join(values)}")
    return "\n".join(parts)


# --- Cross-references ---

def update_cross_references(topic: str, project: str, saved_path: str):
    """Добавить ссылки в связанные статьи."""
    from memory_compiler.search import _embeddings, get_embed_model
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
            continue
        fpath = KNOWLEDGE_DIR / key
        if not fpath.exists() or fpath.name.startswith("_"):
            continue
        text = fpath.read_text(encoding="utf-8")
        ref_line = f"- [{topic}](../{saved_path}) ({now})"
        if "## См. также" in text:
            if saved_path in text:
                continue
            text = text.rstrip() + f"\n{ref_line}\n"
        else:
            text = text.rstrip() + f"\n\n## См. также\n{ref_line}\n"
        fpath.write_text(text, encoding="utf-8")
```

- [ ] **Step 2: Verify storage module**

Run: `cd /path/to/memory-compiler && python -c "from memory_compiler.storage import today_log_path, project_dir, auto_tags; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add memory_compiler/storage.py
git commit -m "refactor: extract storage module (articles, git, utils)"
```

---

### Task 4: Extract Tool Handlers Module

**Files:**
- Create: `memory_compiler/handlers.py`

- [ ] **Step 1: Create `memory_compiler/handlers.py`**

Extract all `async def _save_lesson`, `_get_context`, `_search`, `_compile`, `_lint`, `_save_session`, `_load_session`, `_get_summary`, `_ask`, `_get_active_context`, `_delete_article`, `_edit_article`, `_read_article`, `_search_by_tag`, `_article_history`, `_start_task`, `_finish_task`, `_add_project`, `_remove_project`, `_list_projects` (lines 932-1749). These become the public functions in `handlers.py`.

Import from config, search, storage. All functions keep same signatures but drop `_` prefix where it was used. The file is the largest module (~800 lines) but it's all handler logic with clear function boundaries.

```python
"""MCP tool handler implementations."""
import re
import shutil
from datetime import datetime
from pathlib import Path

from mcp.types import TextContent

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, track_access, article_meta, save_article_meta,
    _discover_projects,
)
from memory_compiler.search import (
    whoosh_search, index_document, embed_document,
    rebuild_index, rebuild_embeddings, _embeddings, _embed_texts,
)
from memory_compiler.storage import (
    today_log_path, project_dir, find_existing_article,
    merge_into_article, regenerate_index, git_commit,
    update_active_context, detect_contradictions,
    auto_tags, extract_git_refs, format_git_refs,
    update_cross_references,
)

# ... paste ALL handler functions here, replacing _prefixed helpers with imported versions.
# Example for save_lesson:

async def save_lesson(topic: str, content: str, project: str, tags: list = None, force_new: bool = False) -> list[TextContent]:
    tags = tags or []
    auto = auto_tags(content, topic)
    existing_lower = {t.lower() for t in tags}
    tags = tags + [t for t in auto if t not in existing_lower]
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    slug = re.sub(r"[^\w\-]", "_", topic.lower())[:50]
    log_path = today_log_path()
    separator = "\n---\n" if log_path.exists() and log_path.stat().st_size > 0 else ""
    entry = f"""{separator}\n## {topic}\n\n**Время:** {ts}\n**Проект:** {project}\n**Теги:** {', '.join(tags) if tags else '—'}\n\n{content}\n"""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
    existing = None if force_new else find_existing_article(topic, content, project)
    if existing:
        merge_into_article(existing, content, tags, ts)
        article_path = existing
        article_text = article_path.read_text(encoding="utf-8")
        action = f"🔄 Обновлено: {project}/{article_path.name}"
    else:
        article_path = project_dir(project) / f"{slug}.md"
        if article_path.exists():
            article_path = project_dir(project) / f"{slug}_{now.strftime('%Y%m%d')}.md"
        article_text = f"""# {topic}\n\n**Дата:** {ts}\n**Проект:** {project}\n**Теги:** {', '.join(tags) if tags else '—'}\n\n## Записи\n\n### {ts}\n{content}\n"""
        article_path.write_text(article_text, encoding="utf-8")
        regenerate_index()
        action = f"✅ Создано: {project}/{article_path.name}"
    git_refs = extract_git_refs(content, topic)
    if git_refs:
        refs_text = format_git_refs(git_refs)
        article_text = article_path.read_text(encoding="utf-8")
        if "## Git-ссылки" not in article_text:
            article_text = article_text.rstrip() + f"\n\n## Git-ссылки\n{refs_text}\n"
        else:
            existing_end = article_text.index("## Git-ссылки") + len("## Git-ссылки")
            article_text = article_text[:existing_end] + f"\n{refs_text}\n"
        article_path.write_text(article_text, encoding="utf-8")
    article_text = article_path.read_text(encoding="utf-8")
    index_document(article_text, article_path.name, project)
    embed_document(article_text, article_path.name, project)
    saved_key = f"{project}/{article_path.name}"
    contradictions = detect_contradictions(content, project, exclude_path=saved_key)
    update_cross_references(topic, project, saved_key)
    update_active_context(project, topic, content)
    track_access([saved_key])
    git_commit(f"save: {topic} [{project}]")
    result = action
    if git_refs:
        refs_summary = ", ".join(f"{k}: {', '.join(v)}" for k, v in git_refs.items())
        result += f"\n🔗 Git: {refs_summary}"
    if contradictions:
        result += "\n\n⚠️ Возможные противоречия:\n" + "\n".join(f"  - {c}" for c in contradictions)
    return [TextContent(type="text", text=result)]

# ... ALL other handler functions follow the same pattern.
# Copy each function from server.py, replace _helper calls with module imports.
# Full list: get_context, search, _parse_daily_entries, compile, lint,
# save_session, load_session, get_summary, ask, get_active_context,
# delete_article, edit_article, read_article, search_by_tag, article_history,
# start_task, finish_task, add_project, remove_project, list_projects
```

Note: This is the largest module. The implementer must copy ALL handler functions from `server.py` lines 932-1749, adjusting imports. Every `_function()` call must be replaced with the imported version (e.g., `_merge_into_article()` -> `merge_into_article()`).

- [ ] **Step 2: Verify handlers module**

Run: `cd /path/to/memory-compiler && python -c "from memory_compiler.handlers import save_lesson, search, compile; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add memory_compiler/handlers.py
git commit -m "refactor: extract handlers module (all tool implementations)"
```

---

### Task 5: Extract MCP Tools and API Modules

**Files:**
- Create: `memory_compiler/tools.py`
- Create: `memory_compiler/api.py`
- Create: `memory_compiler/ui.py`

- [ ] **Step 1: Create `memory_compiler/ui.py`**

Extract the `WEB_HTML` string (lines 1854-2185):

```python
"""Web UI HTML template."""

WEB_HTML = """<!DOCTYPE html>
<html lang="ru">
... (copy entire WEB_HTML string from server.py lines 1854-2185)
</html>"""
```

- [ ] **Step 2: Create `memory_compiler/tools.py`**

Extract `list_tools()` and `call_tool()` (lines 620-929). This is the MCP tool registration:

```python
"""MCP tool definitions and dispatch."""
from mcp.server import Server
from mcp.types import Tool, TextContent

from memory_compiler.config import PROJECTS, stats
from memory_compiler.search import rebuild_index, rebuild_embeddings
from memory_compiler.storage import regenerate_index
from memory_compiler import handlers

app = Server("memory-compiler")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="save_lesson",
            # ... copy all Tool definitions exactly as in server.py
        ),
        # ... all 19 tools
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name in stats:
        stats[name] = stats.get(name, 0) + 1

    # Dispatch to handlers
    dispatch = {
        "save_lesson": lambda: handlers.save_lesson(**arguments),
        "get_context": lambda: handlers.get_context(**arguments),
        "search": lambda: handlers.search(**arguments),
        "compile": lambda: handlers.compile(arguments.get("dry_run", True), arguments.get("project"), arguments.get("since")),
        "lint": lambda: handlers.lint(arguments.get("project", "all"), arguments.get("fix", False)),
        "reindex": None,  # handled inline
        "save_session": lambda: handlers.save_session(**arguments),
        "load_session": lambda: handlers.load_session(**arguments),
        "get_summary": lambda: handlers.get_summary(**arguments),
        "ask": lambda: handlers.ask(**arguments),
        "get_active_context": lambda: handlers.get_active_context(**arguments),
        "delete_article": lambda: handlers.delete_article(**arguments),
        "edit_article": lambda: handlers.edit_article(**arguments),
        "read_article": lambda: handlers.read_article(**arguments),
        "search_by_tag": lambda: handlers.search_by_tag(**arguments),
        "article_history": lambda: handlers.article_history(**arguments),
        "add_project": lambda: handlers.add_project(**arguments),
        "remove_project": lambda: handlers.remove_project(**arguments),
        "list_projects": lambda: handlers.list_projects(),
        "start_task": lambda: handlers.start_task(**arguments),
        "finish_task": lambda: handlers.finish_task(**arguments),
    }

    if name == "reindex":
        count = rebuild_index()
        ecount = rebuild_embeddings()
        regenerate_index()
        result = [TextContent(type="text", text=f"✅ Переиндексировано: {count} документов (BM25F + {ecount} embeddings), index.md обновлён")]
    elif name in dispatch:
        result = await dispatch[name]()
    else:
        result = [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]

    total = sum(len(t.text) for t in result)
    stats["total_chars_returned"] = stats.get("total_chars_returned", 0) + total
    return result
```

- [ ] **Step 3: Create `memory_compiler/api.py`**

Extract all `web_*` functions and `create_starlette_app` (lines 2188-2480):

```python
"""REST API endpoints and Starlette app factory."""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime

import numpy as np
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, article_meta, load_article_meta,
)
from memory_compiler.search import (
    whoosh_search, rebuild_index, rebuild_embeddings,
    load_embeddings, _embeddings, _embed_texts,
)
from memory_compiler.storage import (
    project_dir, regenerate_index, git_init,
    find_existing_article, merge_into_article, auto_tags,
)
from memory_compiler.handlers import compile
from memory_compiler.ui import WEB_HTML


async def web_index(request: Request):
    return HTMLResponse(WEB_HTML)


# ... copy all web_* functions from server.py lines 2192-2410
# They stay exactly the same, just import what they need.


def create_starlette_app(mcp_server: Server) -> Starlette:
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await mcp_server.run(streams[0], streams[1], mcp_server.create_initialization_options())

    async def auto_compile_loop():
        while True:
            now = datetime.now()
            target = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if target <= now:
                target = target.replace(day=target.day + 1)
            wait = (target - now).total_seconds()
            await asyncio.sleep(wait)
            try:
                result = await compile(dry_run=False)
                print(f"Auto-compile: {result[0].text}")
            except Exception as e:
                print(f"Auto-compile error: {e}")

    @asynccontextmanager
    async def lifespan(app):
        git_init()
        load_article_meta()
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
```

- [ ] **Step 4: Commit**

```bash
git add memory_compiler/tools.py memory_compiler/api.py memory_compiler/ui.py
git commit -m "refactor: extract tools, api, and ui modules"
```

---

### Task 6: Replace server.py with Thin Launcher

**Files:**
- Modify: `server.py` (replace 2480 lines with ~15 lines)

- [ ] **Step 1: Replace `server.py` content**

```python
"""memory-compiler MCP server — entry point."""
import os
import uvicorn
from memory_compiler.tools import app
from memory_compiler.api import create_starlette_app

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8765"))
    starlette_app = create_starlette_app(app)
    uvicorn.run(starlette_app, host=host, port=port)
```

- [ ] **Step 2: Verify the server starts**

Run: `cd /path/to/memory-compiler && KNOWLEDGE_DIR=./knowledge python -c "from memory_compiler.tools import app; from memory_compiler.api import create_starlette_app; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "refactor: server.py is now thin launcher (2480 -> 15 lines)"
```

---

### Task 7: Add Pytest Tests

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`
- Create: `tests/test_storage.py`
- Create: `tests/test_search.py`
- Create: `tests/test_handlers.py`

- [ ] **Step 1: Create `tests/conftest.py` with shared fixtures**

```python
"""Shared test fixtures."""
import os
import pytest
from pathlib import Path


@pytest.fixture
def knowledge_dir(tmp_path):
    """Create a temporary knowledge directory with test data."""
    kd = tmp_path / "knowledge"
    kd.mkdir()
    # Create test project
    proj = kd / "testproj"
    proj.mkdir()
    # Create a test article
    article = proj / "test_article.md"
    article.write_text(
        "# Test Article\n\n"
        "**Дата:** 2026-01-01 10:00\n"
        "**Проект:** testproj\n"
        "**Теги:** docker, test\n\n"
        "## Записи\n\n"
        "### 2026-01-01 10:00\n"
        "Test content about docker deployment on NAS.\n",
        encoding="utf-8",
    )
    # Create daily dir
    daily = kd / "daily"
    daily.mkdir()
    return kd


@pytest.fixture(autouse=True)
def patch_knowledge_dir(knowledge_dir, monkeypatch):
    """Patch KNOWLEDGE_DIR for all tests."""
    monkeypatch.setenv("KNOWLEDGE_DIR", str(knowledge_dir))
    # Reimport config to pick up new KNOWLEDGE_DIR
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(cfg, "INDEX_DIR", knowledge_dir / ".whoosh_index")
    monkeypatch.setattr(cfg, "ARTICLE_META_PATH", knowledge_dir / ".article_meta.json")
    monkeypatch.setattr(cfg, "article_meta", {})
    monkeypatch.setattr(cfg, "PROJECTS", ["testproj", "general"])
```

- [ ] **Step 2: Create `tests/test_config.py`**

```python
"""Tests for config module."""
from memory_compiler.config import decay_factor, track_access, article_meta


def test_decay_factor_unknown_path():
    assert decay_factor("nonexistent/file.md") == 0.7


def test_track_access(knowledge_dir):
    track_access(["testproj/test_article.md"])
    assert "testproj/test_article.md" in article_meta
    assert article_meta["testproj/test_article.md"]["access_count"] == 1
    # Second access
    track_access(["testproj/test_article.md"])
    assert article_meta["testproj/test_article.md"]["access_count"] == 2
```

- [ ] **Step 3: Create `tests/test_storage.py`**

```python
"""Tests for storage module."""
import re
from memory_compiler.storage import (
    auto_tags, extract_git_refs, format_git_refs,
    detect_contradictions, merge_into_article,
    project_dir, today_log_path,
)


def test_auto_tags_docker():
    tags = auto_tags("Настроил docker-compose для деплоя", "Docker NAS")
    assert "docker" in tags
    assert "deploy" in tags
    assert "nas" in tags


def test_auto_tags_1c():
    tags = auto_tags("Исправил обработку в 1С", "Баг обработки")
    assert "1c" in tags
    assert "bugfix" in tags


def test_extract_git_refs():
    refs = extract_git_refs("Fixed in abc1234def, see #42", "Bugfix")
    assert "commit" in refs
    assert "issue" in refs
    assert "42" in refs["issue"]


def test_format_git_refs():
    refs = {"commit": ["abc1234"], "issue": ["42"]}
    result = format_git_refs(refs)
    assert "Коммиты" in result
    assert "abc1234" in result


def test_detect_contradictions_no_facts(knowledge_dir):
    warnings = detect_contradictions("просто текст без фактов", "testproj")
    assert warnings == []


def test_project_dir_creates(knowledge_dir):
    p = project_dir("newproj")
    assert p.exists()


def test_today_log_path(knowledge_dir):
    p = today_log_path()
    assert p.parent.exists()
    assert p.suffix == ".md"


def test_merge_into_article(knowledge_dir):
    article = knowledge_dir / "testproj" / "test_article.md"
    original = article.read_text(encoding="utf-8")
    merge_into_article(article, "New content added", ["newtag"], "2026-04-13 12:00")
    updated = article.read_text(encoding="utf-8")
    assert "New content added" in updated
    assert "newtag" in updated
    assert "2026-04-13 12:00" in updated
```

- [ ] **Step 4: Create `tests/test_search.py`**

```python
"""Tests for search module."""
from memory_compiler.search import (
    rebuild_index, _parse_article,
)


def test_parse_article():
    text = "# My Title\n\n**Теги:** docker, test\n\nBody text here"
    result = _parse_article(text, "my_title.md", "testproj")
    assert result["title"] == "My Title"
    assert "docker" in result["tags"]
    assert result["project"] == "testproj"
    assert result["path"] == "testproj/my_title.md"


def test_rebuild_index(knowledge_dir):
    count = rebuild_index()
    assert count >= 1  # at least our test article
```

- [ ] **Step 5: Create `tests/test_handlers.py`**

```python
"""Tests for handler functions."""
import pytest
from memory_compiler.search import rebuild_index, rebuild_embeddings
from memory_compiler.handlers import (
    save_lesson, search, get_context, read_article,
    delete_article, list_projects, add_project, remove_project,
)


@pytest.fixture(autouse=True)
def setup_indexes(knowledge_dir):
    """Rebuild indexes before handler tests."""
    rebuild_index()
    # Skip embeddings in tests for speed — they require model download
    yield


@pytest.mark.asyncio
async def test_save_lesson(knowledge_dir):
    result = await save_lesson("Test Save", "Content for test", "testproj", ["test"])
    assert len(result) == 1
    assert "testproj" in result[0].text


@pytest.mark.asyncio
async def test_search(knowledge_dir):
    result = await search("docker", "testproj")
    assert len(result) == 1
    # Should find our test article
    assert "docker" in result[0].text.lower() or "test" in result[0].text.lower()


@pytest.mark.asyncio
async def test_read_article(knowledge_dir):
    result = await read_article("testproj", "test_article.md")
    assert "Test Article" in result[0].text


@pytest.mark.asyncio
async def test_read_article_not_found(knowledge_dir):
    result = await read_article("testproj", "nonexistent.md")
    assert "не найдена" in result[0].text


@pytest.mark.asyncio
async def test_list_projects(knowledge_dir):
    result = await list_projects()
    assert "testproj" in result[0].text


@pytest.mark.asyncio
async def test_add_and_remove_project(knowledge_dir):
    result = await add_project("newtest")
    assert "newtest" in result[0].text
    result = await remove_project("newtest")
    assert "newtest" in result[0].text
```

- [ ] **Step 6: Create `tests/__init__.py`**

```python
# tests package
```

- [ ] **Step 7: Add pytest dependencies**

Add to `requirements.txt`:
```
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

- [ ] **Step 8: Run tests**

Run: `cd /path/to/memory-compiler && pip install pytest pytest-asyncio && python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 9: Commit**

```bash
git add tests/ requirements.txt
git commit -m "test: add pytest suite for config, storage, search, handlers"
```

---

### Task 8: Update Dockerfile and docker-compose

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Update Dockerfile for package structure**

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout=120 -r requirements.txt

COPY memory_compiler/ memory_compiler/
COPY server.py .

ENV KNOWLEDGE_DIR=/knowledge
ENV MCP_TRANSPORT=sse
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')"

CMD ["python", "server.py"]
```

- [ ] **Step 2: Update docker-compose.yml — remove healthcheck duplication**

```yaml
version: "3.8"

services:
  memory-compiler:
    build: .
    container_name: memory-compiler-mcp
    restart: unless-stopped
    ports:
      - "8765:8765"
    volumes:
      - ./knowledge:/knowledge
      - hf_cache:/root/.cache/huggingface
    environment:
      - KNOWLEDGE_DIR=/knowledge
      - MCP_TRANSPORT=sse
      - MCP_HOST=0.0.0.0
      - MCP_PORT=8765

volumes:
  hf_cache:
```

- [ ] **Step 3: Test Docker build**

Run: `cd /path/to/memory-compiler && docker build -t memory-compiler-test .`
Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "build: update Docker for package structure"
```

---

### Task 9: Update Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README.md with new structure**

Update the project structure section and installation instructions to reflect the package layout:

```markdown
## Project Structure

```
memory-compiler/
├── server.py                  # Entry point
├── memory_compiler/
│   ├── __init__.py
│   ├── config.py              # Constants, shared state, metadata
│   ├── search.py              # Whoosh BM25F + semantic search
│   ├── storage.py             # Articles, git, utils, auto-tagging
│   ├── handlers.py            # MCP tool implementations
│   ├── tools.py               # MCP tool definitions & dispatch
│   ├── api.py                 # REST endpoints, Starlette app
│   └── ui.py                  # Web UI HTML template
├── tests/
│   ├── conftest.py            # Shared fixtures
│   ├── test_config.py
│   ├── test_storage.py
│   ├── test_search.py
│   └── test_handlers.py
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Running Tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: update README for package structure"
```

---

### Task 10: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `cd /path/to/memory-compiler && python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 2: Verify server starts locally**

Run: `cd /path/to/memory-compiler && KNOWLEDGE_DIR=./knowledge timeout 10 python server.py || true`
Expected: Server starts, prints "Whoosh index built" before timeout

- [ ] **Step 3: Run existing integration tests**

Run: `cd /path/to/memory-compiler && python test_all.py`
Expected: All existing tests pass (requires running server)

- [ ] **Step 4: Docker build verification**

Run: `cd /path/to/memory-compiler && docker build -t memory-compiler-test .`
Expected: Build succeeds

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "refactor: complete migration to package structure"
```
