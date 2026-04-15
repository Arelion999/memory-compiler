"""
Configuration, constants, schema, and article metadata for memory-compiler.
"""
import json
import os
from datetime import datetime
from pathlib import Path

from whoosh.fields import Schema, TEXT, ID, STORED
from whoosh.analysis import RegexTokenizer, LowercaseFilter

# ─── Paths & constants ───────────────────────────────────────────────────────

KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR", "/knowledge"))
INDEX_DIR = KNOWLEDGE_DIR / ".whoosh_index"
_INITIAL_PROJECTS = os.environ.get("PROJECTS", "general").split(",")
_HIDDEN_DIRS = {".whoosh_index", ".git", "daily"}

# Auth & encryption
MC_API_KEY = os.environ.get("MC_API_KEY", "")
MC_ENCRYPT_KEY = os.environ.get("MC_ENCRYPT_KEY", "")


# ─── Version ─────────────────────────────────────────────────────────────────

def _read_version() -> str:
    for candidate in [
        Path(__file__).parent.parent / "VERSION",  # repo root
        Path("/app/VERSION"),  # docker container
    ]:
        try:
            if candidate.exists():
                return candidate.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return "0.0.0-unknown"


VERSION = _read_version()


def _discover_projects() -> list[str]:
    """Collect project list from existing folders + initial."""
    found = set(_INITIAL_PROJECTS)
    if KNOWLEDGE_DIR.exists():
        for d in KNOWLEDGE_DIR.iterdir():
            if d.is_dir() and d.name not in _HIDDEN_DIRS and not d.name.startswith("."):
                found.add(d.name)
    return sorted(found)


# Dynamic list — updated on add/remove
PROJECTS = _discover_projects()

# ─── Whoosh schema ───────────────────────────────────────────────────────────

analyzer = RegexTokenizer(r'[\w]{2,}') | LowercaseFilter()
SCHEMA = Schema(
    path=ID(stored=True, unique=True),
    project=ID(stored=True),
    title=TEXT(stored=True, analyzer=analyzer, field_boost=5.0),
    tags=TEXT(stored=True, analyzer=analyzer, field_boost=3.0),
    body=TEXT(analyzer=analyzer, field_boost=1.0),
    preview=STORED,
)

# ─── Usage stats ─────────────────────────────────────────────────────────────

stats = {"search": 0, "save": 0, "get_context": 0, "compile": 0, "lint": 0, "total_chars_returned": 0}

# ─── Article metadata (temporal decay + analytics) ───────────────────────────

ARTICLE_META_PATH = KNOWLEDGE_DIR / ".article_meta.json"
article_meta: dict[str, dict] = {}  # path -> {last_accessed, access_count, created}


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
        return 0.7  # neutral for unknown
    try:
        last = datetime.fromisoformat(meta["last_accessed"])
        days = (datetime.now() - last).days
        return max(0.3, 1.0 / (1.0 + days / 30.0))
    except Exception:
        return 0.7
