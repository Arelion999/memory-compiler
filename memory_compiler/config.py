"""
Configuration, constants, schema, and article metadata for memory-compiler.
"""
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path


# ─── Secret detection ────────────────────────────────────────────────────────
# Признак секретности — флаг СТРОГО в меташапке (как его пишет save_secret,
# отдельной строкой рядом с **Дата:**/**Теги:**) и/или префикс имени secret_.
# Раньше проверка была подстрокой `"**Секрет:** да" in text` и ловила флаг где
# угодно — в теле, инлайн-коде, документации про секреты (баг 1.7.27): обычные
# статьи молча шифровались и выпадали из поиска. Единый helper (DRY) исключает
# расхождение между точками проверки.

SECRET_FLAG = "**Секрет:** да"


def is_secret_article(text: str, filename: str) -> bool:
    """True, если статья секретная: имя начинается с secret_ ИЛИ флаг SECRET_FLAG
    стоит отдельной строкой в меташапке (до первого '## ' или пустой строки после
    метаблока). Упоминание флага в теле признаком не является."""
    if filename.startswith("secret_"):
        return True
    meta_started = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## "):
            break
        if s == "" and meta_started:
            break
        if s == SECRET_FLAG:  # точное совпадение строки, не подстрока
            return True
        if re.match(r"\*\*.+?:\*\*", s):
            meta_started = True
    return False


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
# 'logs' — каталог структурного лога сервера (obs.py пишет туда app.jsonl), а не проект.
# Числясь проектом, он попадал в list_projects как «0 статей» и линтовался вхолостую.
_HIDDEN_DIRS = {".whoosh_index", ".git", "daily", "logs"}

# Auth & encryption
MC_API_KEY = os.environ.get("MC_API_KEY", "")
MC_ENCRYPT_KEY = os.environ.get("MC_ENCRYPT_KEY", "")


# ─── ML-модели по умолчанию ──────────────────────────────────────────────────
# Единый источник для search.py (загрузка моделей) и hf_offline.py (проверка кеша ДО
# импорта ML-библиотек). hf_offline не может взять их из search: тот при импорте тянет
# sentence_transformers, а с ним huggingface_hub, и офлайн-режим замёрз бы до решения.
DEFAULT_EMBED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


def env_flag(name: str, environ=None) -> bool:
    """Флаг из env: «1», «true» или «yes» без учёта регистра; иначе False."""
    env = os.environ if environ is None else environ
    return env.get(name, "false").lower() in ("1", "true", "yes")


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


PROBE_LEVELS = ("reachable", "verified", "stale")
PROBE_VERDICTS = ("verified", "stale")
# Потолок числа цитат, по которым храним вердикт. Цитаты переписывают, и ключей за годы
# накопится больше, чем цитат в статье: вердикт удалённой команды всё равно не учитывается
# при выдаче (reflexes.probe_stamp_of), но занимал бы место в сайдкаре.
PROBE_CHECKS_MAX = 20


def probe_command_key(command: str) -> str:
    """Ключ вердикта — команда цитаты со схлопнутыми пробелами.

    ⚠️ НОРМАЛИЗАЦИЯ ОДНА на запись и на чтение (reflexes.probe_stamp_of): хук шлёт команду
    так, как прочитал её из карточки, а в статье она может стоять с двойным пробелом.
    Разойдутся — вердикт молча не найдёт свою цитату, и карточка промолчит."""
    return " ".join((command or "").split())


def probe_stamp(key: str, level: str, save: bool = True, command: str | None = None) -> bool:
    """Штамп живой проверки факта: key — «проект/файл.md», как в track_access.
    Возвращает True, если штамп лёг.

    В теле статьи штампа нет сознательно: он меняется на каждую команду к железу, а
    запись в статью тянет git add -A (5,5 с на всю базу).

    `save=False` — проставить штамп без записи файла: вызывающий сохранит сайдкар один
    раз за запрос. Полная перезапись .article_meta.json на каждую статью, да ещё
    синхронно в event loop, — тот самый класс, которым сервер уже вешали дважды.

    ⚠️ REACHABLE НЕ ЗАТИРАЕТ ВЕРДИКТ ПО ФАКТУ (v1.81.1). «Узел отвечает» — свойство цели,
    verified/stale — свойство цитаты конкретной статьи, а слот у статьи один. Пока писал
    последний, вердикт жил до следующей команды к узлу: живая проверка 14.09.2026 —
    verified в 19:20:58, через секунду команда без цитаты прислала reachable по той же
    цели, и в сайдкаре остался reachable. Карточка показывает ТОЛЬКО verified и stale
    (reflexes.render), так что пропадал единственный видимый сигнал, а stale —
    предупреждение о протухшем факте — стирался первой же посторонней командой. Сменить
    вердикт может только новый вердикт, то есть повторный прогон цитаты. Битый штамп
    вердиктом не считается: карточка его не показывает (probe_stamp_of), и запрет
    перезаписи оставил бы его навсегда.
    ⚠️ ВЕРДИКТ — СВОЙСТВО ЦИТАТЫ, А НЕ СТАТЬИ (v1.82.0). Цитат у статьи несколько, а слот
    был один: verified по цитате B затирал stale по цитате A, и предупреждение исчезало,
    хотя статья продолжала врать. `command` — команда исполненной цитаты, её присылает хук;
    вердикт ложится в `checks[ключ команды]`, и карточка считает по НИМ
    (reflexes.probe_stamp_of). Прежний слот `last_probe` пишется КАК И РАНЬШЕ: хук
    выкатывается после сервера, и вердикт без команды обязан работать по-старому."""
    if level not in PROBE_LEVELS:
        return False
    entry = article_meta.setdefault(key, {"access_count": 0, "created": datetime.now().isoformat()})
    current = entry.get("last_probe")
    if (level == "reachable" and isinstance(current, dict)
            and current.get("level") in PROBE_VERDICTS and current.get("date")):
        return False
    stamp = {"date": datetime.now().isoformat(timespec="seconds"), "level": level}
    entry["last_probe"] = stamp
    # «Узел жив» цитаты не касается: слот цитаты им не занимаем и вердикт по ней не трогаем.
    cmd_key = probe_command_key(command) if level in PROBE_VERDICTS else ""
    if cmd_key:
        checks = entry.get("checks")
        checks = dict(checks) if isinstance(checks, dict) else {}
        checks.pop(cmd_key, None)       # повторный прогон уводит цитату в конец очереди
        checks[cmd_key] = dict(stamp)
        # ⚠️ Потолок по ПОРЯДКУ ЗАПИСИ, а не по дате: штампы одной секунды неразличимы, и
        # выбор «самого старого» среди них зависел бы от обхода словаря.
        entry["checks"] = dict(list(checks.items())[-PROBE_CHECKS_MAX:])
    if save:
        save_article_meta()
    return True


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
