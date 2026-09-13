"""Рефлексы памяти (v1.78.0): памятка из базы приходит сама в момент действия агента.

Статья объявляет, при чём ей всплывать, разделом:

    ## Рефлексы
    - ошибка: Unable to start subsystem: sftp
    - цель: 192.0.2.10
    - файл: memory_compiler/ui.py

Хук Claude Code шлёт событие в POST /api/reflex — текст упавшей команды, цель выхода
на железо или путь прочитанного файла, — и кладёт найденное в контекст модели.
Замер 13.09.2026 (873 сессии): 225 из 432 ошибок повторяющихся классов случились,
когда фикс уже лежал в базе; после ошибки в базу заходили в 13% случаев.

Каналы: явный триггер статьи; для цели ещё и адрес в заголовке статьи — так
приходят указатели на секреты с доступами к этой цели.

⚠️ ПОДБОРА ПО УПОМИНАНИЮ СЛОВА НЕТ И НЕ ДОБАВЛЯТЬ. Проверено 13.09.2026: из шести
свежих статей с «charmap/cp1251» ни одна не про починку — слово стоит мимоходом.

⚠️ ДОСЛОВНЫЙ КАНАЛ ДЛЯ ОШИБОК ПРОВЕРЕН И ОТВЕРГНУТ (13.09.2026, копия базы, 1072
уникальные ошибки из транскриптов): памятку получили 18 ошибок, из них ~12 — шум.
Общие сообщения («Подключение не установлено…», «Ошибка соединения с сервером»)
дословно стоят в логах, процитированных статьями. Для ошибки нужен явный триггер.

⚠️ НОРМАЛИЗАЦИЯ ОДНА на индекс и на запрос: иначе триггер и живая ошибка молча
разойдутся на кавычках, путях и числах.

⚠️ СКАН НЕ ДЕРЖИТ ВЫЗЫВАЮЩИХ. invalidate() без замка — её зовут async-хендлеры
записи; refresh_index() без force при занятом замке отдаёт текущий снимок. Иначе
холодный скан после рестарта вешал бы event loop и забивал пул потоков сервера.
"""
import os
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

import memory_compiler.config as cfg
from memory_compiler.search import SERVICE_FILES
from memory_compiler.storage import (
    SUPERSEDED_MARK, _parse_frontmatter, article_body_lines, article_title_tags,
    parse_meta_value,
)

KINDS = ("error", "target", "file")
KIND_RU = {"error": "ошибка", "target": "цель", "file": "файл"}
_KIND_ALIASES = {"ошибка": "error", "error": "error", "цель": "target",
                 "target": "target", "файл": "file", "file": "file"}
SECTION_TITLES = ("## Рефлексы", "## Reflexes")
GIT_REFS_HEADING = "## Git-ссылки"

REFLEX_RESCAN_SEC = 30      # чаще раза в N секунд базу не обходим
MEMO_LIMIT = 3
RENDER_BUDGET = 1500
SNIPPET_MAX = 200
TEXT_MAX = 4000             # символов на один текст события
KEY_LINES_MAX = 15
MIN_ERROR_TRIGGER = 12
MIN_TARGET = 4
# Публичные резолверы пингуют ради проверки связи: статьи с ними в заголовке — не про
# цель (замер 13.09.2026: 8.8.8.8 в 179 командах давал 3 памятки мимо).
TARGET_STOP = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1",
                         "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9"})
SKIP_DIRS = frozenset(cfg._HIDDEN_DIRS) | {"archive"}

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LINE_RE = re.compile(r"^\s*[-*]\s+([^\s:]+)\s*:\s*(.+?)\s*$")
_ARG_RE = re.compile(r"^\s*([^\s:]+)\s*:\s*(.+?)\s*$")
# Канал «цель по заголовку» — только для похожего на адрес: IP, домен, хост с цифрой
# или дефисом. Простые слова из полей MCP («admin», «bridge») совпадали с заголовками
# чужих секретов (ревью 13.09.2026: «admin» — 10 статей, «bridge» — 4).
_ADDRESS_LIKE_RE = re.compile(r"[.:\d-]")
_META_LABEL_RE = re.compile(r"^\*\*[^*]+:\*\*")


# ─── нормализация ────────────────────────────────────────────────────────────
_WIN_PATH_RE = re.compile(r"(?<![a-z0-9])[a-z]:[\\/][^\s\"'<>|]*")
_UNIX_PATH_RE = re.compile(r"(?<![\w<])/(?:[\w.\-]+/)+[\w.\-]*")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b")
_NUM_RE = re.compile(r"\d+")
_QUOTES_RE = re.compile("[\"'`«»“”„‘’]")
_WS_RE = re.compile(r"\s+")


def normalize_error(s: str) -> str:
    """Текст ошибки к виду, где триггер и живая ошибка совпадают подстрокой."""
    s = (s or "").casefold()
    s = _WIN_PATH_RE.sub("<p>", s)
    s = _UNIX_PATH_RE.sub("<p>", s)
    s = _UUID_RE.sub("<u>", s)
    s = _HEX_RE.sub("<h>", s)
    s = _NUM_RE.sub("<n>", s)
    s = _QUOTES_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip()


def normalize_file(s: str) -> str:
    s = (s or "").strip().replace("\\", "/").casefold()
    s = re.sub(r"/{2,}", "/", s)
    while s.startswith("./"):
        s = s[2:]
    return s


def normalize_target(s: str) -> str:
    return (s or "").strip().casefold().rstrip(".")


NORMALIZE = {"error": normalize_error, "target": normalize_target, "file": normalize_file}


def trigger_problem(kind: str, value: str):
    """Почему триггер не годится, или None."""
    if not 3 <= len(value) <= 300:
        return "длина значения должна быть 3..300 символов"
    # \r, U+2028 и прочие разрывы строки рвали бы разметку статьи (ревью 13.09.2026):
    # точка в регулярке их пропускает, а read_text и merge_into_article — нет.
    if len(value.splitlines()) != 1 or any(
            ch != "\t" and unicodedata.category(ch)[0] == "C" for ch in value):
        return "значение — одна строка без управляющих символов"
    norm = NORMALIZE[kind](value)
    if kind == "error" and len(norm) < MIN_ERROR_TRIGGER:
        return "слишком общая строка ошибки — нужна дословная строка подлиннее"
    if kind == "target" and (len(norm) < MIN_TARGET or norm in TARGET_STOP):
        return "слишком общая цель"
    if kind == "file" and (norm.endswith("/") or "/" not in norm):
        return ("нужен путь файла с каталогом (например memory_compiler/ui.py): "
                "голое имя сработает на одноимённый файл любого репозитория")
    return None


# ─── раздел «## Рефлексы» ────────────────────────────────────────────────────
def _section_bounds(lines: list) -> list:
    """[(начало, конец)] разделов «## Рефлексы» вне блоков кода; конец не включается.

    Граница — ближайший «## » или «### »: merge_into_article дописывает записи в
    конец файла, и без этой границы раздел проглотил бы их."""
    out, in_fence, i = [], False, 0
    while i < len(lines):
        if _FENCE_RE.match(lines[i]):
            in_fence = not in_fence
        elif not in_fence and lines[i].strip() in SECTION_TITLES:
            j = i + 1
            while (j < len(lines) and not lines[j].startswith(("## ", "### "))
                   and not _FENCE_RE.match(lines[j])):
                j += 1
            out.append((i, j))
            i = j
            continue
        i += 1
    return out


def _outside_fences(lines: list):
    """Индексы строк вне блоков кода."""
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            yield i


def parse_triggers(text: str) -> list:
    """[(вид, значение)] из разделов «## Рефлексы»; негодные и дубли отброшены."""
    lines = (text or "").split("\n")
    seen, out = set(), []
    for start, end in _section_bounds(lines):
        for line in lines[start + 1:end]:
            m = _LINE_RE.match(line)
            kind = _KIND_ALIASES.get(m.group(1).casefold()) if m else None
            if not kind or trigger_problem(kind, m.group(2)):
                continue
            key = (kind, NORMALIZE[kind](m.group(2)))
            if key not in seen:
                seen.add(key)
                out.append((kind, m.group(2)))
    return out


def add_triggers(text: str, triggers) -> tuple:
    """Дописать триггеры в раздел «## Рефлексы», создав его при необходимости.

    Возвращает (текст, добавлено [(вид, значение)], отвергнуто [(строка, причина)]).
    Новый раздел встаёт ПЕРЕД «## Git-ссылки» (вне блоков кода), если он есть, иначе
    в конец файла. Строку вместо списка принимаем как один триггер."""
    if isinstance(triggers, str):
        triggers = [triggers]
    added, rejected, fresh = [], [], []
    known = {(k, NORMALIZE[k](v)) for k, v in parse_triggers(text)}
    for item in triggers or []:
        m = _ARG_RE.match(item) if isinstance(item, str) else None
        kind = _KIND_ALIASES.get(m.group(1).casefold()) if m else None
        if not kind:
            rejected.append((str(item), "нужен вид: «ошибка: …», «цель: …» или «файл: …»"))
            continue
        value = m.group(2)
        problem = trigger_problem(kind, value)
        if problem:
            rejected.append((item, problem))
            continue
        key = (kind, NORMALIZE[kind](value))
        if key in known:
            continue
        known.add(key)
        added.append((kind, value))
        fresh.append(f"- {KIND_RU[kind]}: {value}")
    if not fresh:
        return text, added, rejected
    lines = (text or "").rstrip("\n").split("\n")
    bounds = _section_bounds(lines)
    if bounds:
        start, end = bounds[0]
        at = end
        while at - 1 > start and not lines[at - 1].strip():
            at -= 1
        lines[at:at] = fresh
    else:
        git = next((i for i in _outside_fences(lines) if lines[i].strip() == GIT_REFS_HEADING), None)
        if git is None:
            lines += ["", SECTION_TITLES[0], *fresh]
        else:
            block = [SECTION_TITLES[0], *fresh, ""]
            if git > 0 and lines[git - 1].strip():
                block.insert(0, "")
            lines[git:git] = block
    new_text = "\n".join(lines) + "\n"
    # Страховка: триггер, которого разбор не видит в итоговом тексте (например, файл
    # кончается незакрытым блоком кода), мёртв — такой текст не пишем.
    visible = {(k, NORMALIZE[k](v)) for k, v in parse_triggers(new_text)}
    if any((k, NORMALIZE[k](v)) not in visible for k, v in added):
        return text, [], rejected + [(f"{KIND_RU[k]}: {v}", "раздел не удалось вписать в статью")
                                     for k, v in added]
    return new_text, added, rejected


def describe_added(added, rejected) -> str:
    """Строка для ответа инструмента: что легло в раздел и что отвергнуто."""
    parts = []
    if added:
        kinds = ", ".join(dict.fromkeys(KIND_RU[k] for k, _ in added))
        parts.append(f"🧷 Рефлексы: +{len(added)} ({kinds})")
    for item, why in rejected:
        parts.append(f"⚠️ Триггер «{str(item)[:80]}» не принят: {why}")
    return "\n".join(parts)


# ─── ключевые строки ошибки ──────────────────────────────────────────────────
_EXIT_RE = re.compile(r"^exit code -?\d+$", re.I)
_TRUNC_RE = re.compile(r"\.\.\.\s*\[\d+ characters truncated\]\s*\.\.\.")
_FRAME_RE = re.compile(r'^\s*File ".*", line \d+')
_CARET_RE = re.compile(r"^[\s^~]+$")


def _clip(text: str) -> str:
    """Голова и хвост: трейсбек кончается ошибкой, а длинный вывод начинается командой."""
    return text if len(text) <= TEXT_MAX else text[:1000] + "\n" + text[-(TEXT_MAX - 1000):]


def error_key_lines(error: str) -> list:
    """Содержательные строки ошибки: без «Exit code», кадров трейсбека и подчёркиваний."""
    text = _TRUNC_RE.sub("\n", _clip(error or ""))
    out = []
    for raw in text.replace("\r", "").split("\n"):
        s = raw.strip()
        if s and not _EXIT_RE.match(s) and not _FRAME_RE.match(raw) and not _CARET_RE.match(s):
            out.append(s)
    return out[-KEY_LINES_MAX:]


# ─── индекс статей ───────────────────────────────────────────────────────────
@dataclass
class Memo:
    project: str
    file: str
    title: str
    date: str
    verified: str
    secret: bool
    via: str        # trigger | title
    snippet: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Article:
    key: str
    project: str
    file: str
    title: str
    title_norm: str
    date: str
    verified: str
    secret: bool
    triggers: list
    snippet: str


class _Index:
    def __init__(self):
        self.root = None
        self.sigs = {}          # путь -> (mtime_ns, ctime_ns, size)
        self.articles = {}      # путь -> _Article; словарь заменяется целиком, не правится
        self.scanned = None     # time.monotonic() последнего обхода; None — пересканировать


_lock = threading.Lock()
_index = _Index()
_generation = 0


def _snippet(body: str) -> str:
    """Первая содержательная строка тела. Строка-метка «**Тип:** decision» пропускается,
    у метки с длинным значением берётся само значение."""
    for line in article_body_lines(body, limit=12):
        s = line.strip()
        if _META_LABEL_RE.match(s):
            value = parse_meta_value(s)
            if len(value) < 20:
                continue
            s = value
        s = s.lstrip("-*> ").strip()
        if not s or s.startswith(("#", "---", "```", "~~~", "|", "ENC:")):
            continue
        return s[:SNIPPET_MAX]
    return ""


def _read_article(path: Path, project: str):
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        body = _parse_frontmatter(raw)[1]
    except Exception:
        body = raw
    head = body.split("\n")[:14]
    # Метку отмены mark_superseded при длинном frontmatter ставит строкой 1 — внутрь
    # него (ревью 13.09.2026), поэтому смотрим и начало сырого файла.
    if any(l.startswith(SUPERSEDED_MARK) for l in head + raw.split("\n")[:14]):
        return None
    date = verified = ""
    for line in head:
        if line.startswith("**Обновлено:**"):
            date = parse_meta_value(line)
        elif line.startswith("**Дата:**") and not date:
            date = parse_meta_value(line)
        elif line.startswith("**Проверено:**"):
            verified = parse_meta_value(line)
    try:
        title = article_title_tags(raw, fallback=path.stem)[0]
    except Exception:
        title = path.stem
    secret = cfg.is_secret_article(raw, path.name)
    return _Article(
        key=f"{project}/{path.name}", project=project, file=path.name, title=title,
        title_norm=title.casefold(), date=date[:10], verified=verified[:80], secret=secret,
        triggers=[(k, NORMALIZE[k](v)) for k, v in parse_triggers(body)],
        snippet="" if secret else _snippet(body),
    )


def refresh_index(force: bool = False) -> _Index:
    """Пересобрать индекс по изменившимся файлам (mtime, ctime, размер).

    ТЯЖЁЛЫЙ проход по всей базе: из async-кода только через asyncio.to_thread (внесён
    в HEAVY test_no_blocking_calls). Без force при занятом замке отдаёт текущий снимок
    — параллельный запрос не ждёт чужой скан. ctime в подписи ловит правку, которую
    Drive привёз со старым mtime и тем же размером (на NAS замена файла меняет inode)."""
    root = Path(cfg.KNOWLEDGE_DIR)
    if (not force and _index.root == root and _index.scanned is not None
            and time.monotonic() - _index.scanned < REFLEX_RESCAN_SEC):
        return _index
    if not _lock.acquire(blocking=force):
        return _index if _index.root == root else _Index()
    try:
        generation = _generation
        same_root = _index.root == root
        sigs = dict(_index.sigs) if same_root else {}
        articles = dict(_index.articles) if same_root else {}
        seen = set()
        try:
            pdirs = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            pdirs = []
        for pdir in pdirs:
            if pdir.name.startswith(".") or pdir.name in SKIP_DIRS:
                continue
            try:
                entries = list(os.scandir(pdir))
            except OSError:
                continue
            for entry in entries:
                if (not entry.name.endswith(".md") or entry.name in SERVICE_FILES
                        or not entry.is_file()):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                seen.add(entry.path)
                sig = (st.st_mtime_ns, st.st_ctime_ns, st.st_size)
                if sigs.get(entry.path) == sig:
                    continue
                sigs[entry.path] = sig
                art = _read_article(Path(entry.path), pdir.name)
                if art is None:
                    articles.pop(entry.path, None)
                else:
                    articles[entry.path] = art
        for gone in set(sigs) - seen:
            sigs.pop(gone, None)
            articles.pop(gone, None)
        _index.root, _index.sigs, _index.articles = root, sigs, articles
        # invalidate() во время скана не теряется: файл мог быть прочитан до записи
        _index.scanned = time.monotonic() if generation == _generation else None
        return _index
    finally:
        _lock.release()


def invalidate() -> None:
    """Следующий запрос пересканирует базу, не дожидаясь REFLEX_RESCAN_SEC.

    ⚠️ БЕЗ ЗАМКА: её зовут async-хендлеры записи. С замком она ждала бы идущий скан
    (холодный — секунды на NAS) прямо в event loop и вешала сервер целиком."""
    global _generation
    _generation += 1
    _index.scanned = None


# ─── памятки ─────────────────────────────────────────────────────────────────
def _texts(text) -> list:
    items = text if isinstance(text, list) else [text]
    return [_clip(t) for t in items[:3] if isinstance(t, str) and t.strip()]


def _token_in(needle: str, hay: str) -> bool:
    """needle — отдельным токеном: по краям не буква, не цифра, не точка и не дефис."""
    return re.search(r"(?<![\w.\-])" + re.escape(needle) + r"(?![\w.\-])", hay) is not None


def find_memos(kind: str, text, cwd: str = "", exclude=None, limit: int = MEMO_LIMIT) -> list:
    """Памятки по событию хука. ТЯЖЁЛЫЙ вызов (может пересобрать индекс) — через to_thread."""
    if kind not in KINDS:
        raise ValueError(f"unknown reflex kind: {kind}")
    texts = _texts(text)
    if not texts:
        return []
    # Отложенно: handlers импортирует этот модуль, прямой импорт дал бы цикл.
    from memory_compiler.handlers import _project_from_cwd
    home = _project_from_cwd(cwd or "")
    excluded = set(exclude or [])
    found = {}

    def consider(art, rank, via):
        if art.key not in excluded and (art.key not in found or rank < found[art.key][0]):
            found[art.key] = (rank, via, art)

    articles = list(refresh_index().articles.values())
    if kind == "error":
        joined = "\n".join(normalize_error(line) for t in texts for line in error_key_lines(t))
        for art in articles:
            if any(k == "error" and v in joined for k, v in art.triggers):
                consider(art, 0, "trigger")
    elif kind == "target":
        wanted = [w for w in (normalize_target(t) for t in texts)
                  if len(w) >= MIN_TARGET and w not in TARGET_STOP]
        titled = [w for w in wanted if _ADDRESS_LIKE_RE.search(w)]
        for art in articles:
            if any(k == "target" and v in wanted for k, v in art.triggers):
                consider(art, 0, "trigger")
            elif any(_token_in(w, art.title_norm) for w in titled):
                consider(art, 1, "title")
    else:
        paths = [normalize_file(t) for t in texts]
        for art in articles:
            if any(k == "file" and (p == v or p.endswith("/" + v))
                   for k, v in art.triggers for p in paths):
                consider(art, 0, "trigger")

    # Порядок: канал → статьи своего проекта → (для цели) секрет-указатель на доступы
    # → новее. Секрет выше даты сознательно: к цели нужнее всего доступы, а не новости.
    ranked = sorted(found.values(), key=lambda item: item[2].key)
    ranked.sort(key=lambda item: item[2].date, reverse=True)
    ranked.sort(key=lambda item: (item[0], item[2].project != home,
                                  not (kind == "target" and item[2].secret)))
    return [Memo(a.project, a.file, a.title, a.date, a.verified, a.secret, via, a.snippet)
            for _rank, via, a in ranked[:max(1, limit)]]


def describe(kind: str, text) -> str:
    """Что подставить в заголовок памятки: цель или имя файла."""
    items = _texts(text)
    if not items or kind == "error":
        return ""
    if kind == "file":
        return normalize_file(items[0]).rsplit("/", 1)[-1]
    return items[0].strip()[:80]


_HEADERS = {
    "error": "Память (рефлекс по ошибке): база уже знает об этом — прочитай, прежде чем чинить заново.",
    "target": "Память (рефлекс по цели {what}): про это в базе есть записи — прочитай, прежде чем действовать.",
    "file": "Память (рефлекс к файлу {what}): к нему в базе есть пометки — прочитай, прежде чем править.",
}


def render(kind: str, memos: list, what: str = "") -> str:
    """Текст для additionalContext хука; не длиннее RENDER_BUDGET."""
    if not memos:
        return ""
    parts = [_HEADERS[kind].format(what=what)]
    used = len(parts[0])
    for m in memos:
        meta = " · ".join(x for x in (m.date, f"проверено: {m.verified}" if m.verified else "") if x)
        lines = [f"• [{m.project}] {m.title}" + (f" ({meta})" if meta else "")]
        if m.secret:
            lines.append("  секрет: содержимое откроет read_article")
        elif m.snippet:
            lines.append("  " + m.snippet)
        lines.append(f'  → read_article("{m.project}", "{m.file}")')
        block = "\n".join(lines)
        if used + 1 + len(block) > RENDER_BUDGET:
            break
        parts.append(block)
        used += 1 + len(block)
    return "\n".join(parts) if len(parts) > 1 else ""


def shown_in(memos: list, text: str) -> list:
    """Памятки, реально попавшие в текст; не влезшие в бюджет хук не должен считать показанными."""
    return [m for m in memos if f'read_article("{m.project}", "{m.file}")' in text]
