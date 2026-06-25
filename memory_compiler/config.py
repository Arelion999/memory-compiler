"""
Configuration, constants, schema, and article metadata for memory-compiler.
"""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


# ─── Atomic file writes ──────────────────────────────────────────────────────
# Деплой = docker restart, который может прервать запись на полуслове. Прямой
# write_text оставляет обрезанный файл; для .article_meta.json это означает потерю
# ВСЕЙ аналитики (json.loads падает → article_meta = {}). tmp-в-том-же-каталоге +
# os.replace атомарен на POSIX и Windows: читатель видит либо старый файл целиком,
# либо новый целиком, никогда — наполовину. Также защищает от торн-райта при гонке.

def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path, data: bytes) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

from whoosh.fields import Schema, TEXT, ID, STORED
from whoosh.analysis import RegexTokenizer, LowercaseFilter, StemFilter
from whoosh.lang.snowball import russian as ru_snowball, english as en_snowball

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
    """Collect project list from existing folders + initial.

    Names are lowercased — case-variant duplicates collapse to one entry.
    Use storage.merge_case_duplicates() at startup to migrate filesystem.
    """
    found = set(p.strip().lower() for p in _INITIAL_PROJECTS if p.strip())
    if KNOWLEDGE_DIR.exists():
        for d in KNOWLEDGE_DIR.iterdir():
            if d.is_dir() and d.name not in _HIDDEN_DIRS and not d.name.startswith("."):
                found.add(d.name.lower())
    return sorted(found)


# Dynamic list — updated on add/remove
PROJECTS = _discover_projects()

# ─── Whoosh schema ───────────────────────────────────────────────────────────

def _bilingual_stem(word: str) -> str:
    """Stem token using Russian or English Snowball based on character set.

    Snowball stemmers reduce inflected forms to a common base — boosts recall
    for query/document vocabulary mismatch (настройка ↔ настроить, deploys ↔ deploy).
    """
    if not word:
        return word
    # Detect Cyrillic — apply Russian Snowball
    has_cyrillic = any('Ѐ' <= ch <= 'ӿ' for ch in word)
    try:
        if has_cyrillic:
            return ru_snowball.RussianStemmer().stem(word)
        return en_snowball.EnglishStemmer().stem(word)
    except Exception:
        return word


class _BilingualStemFilter(StemFilter):
    """Custom StemFilter routing each token to ru/en stemmer by script."""
    def __init__(self):
        super().__init__(stemfn=_bilingual_stem, ignore=None, cachesize=50000)


# Bilingual analyzer: tokenize → lowercase → stem (RU + EN).
# Whoosh Snowball stemmers reduce inflected forms — cross-form recall for free.
analyzer = RegexTokenizer(r'[\w]{2,}') | LowercaseFilter() | _BilingualStemFilter()
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
    atomic_write_text(ARTICLE_META_PATH, json.dumps(article_meta, ensure_ascii=False, indent=2))


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
