"""Журнал сессий, открытые вопросы и накопленные факты проекта.

Вынесено из storage.py в v1.81.0: файл дорос до 3503 строк и 113 функций.
Шов выбран замером связности — домен цельный и держится на своих файлах
(`_session.md`, `_questions.md`, `_reflections.md`), а из ядра storage берёт
ровно разрешение путей.

⚠️ МОДУЛЬ НЕ ДЕРЖИТ СВОЕГО KNOWLEDGE_DIR, и это не забывчивость. Пути к файлам
домена берутся у `storage.safe_project_path` / `storage.project_path` (чтение) и
`storage.safe_project_dir` / `storage.project_dir` (запись: те создают каталог), которые
читают KNOWLEDGE_DIR из СВОИХ globals в момент вызова. Шесть тестов домена
патчат именно `storage.KNOWLEDGE_DIR` (test_session_journal, test_session_note,
test_question_lifecycle, test_reflections_read, test_first_touch_context,
test_service_files_out_of_index), а conftest — `storage`/`search`/`handlers`/
`api`/`maintenance`/`handlers_reports` поимённо. Заведи здесь своё
`from config import KNOWLEDGE_DIR` — и журнал уйдёт писать в БОЕВУЮ базу мимо
tmp_path, причём МОЛЧА: исключения не будет, просто файлы лягут не туда. Ровно
этот класс поймали на разрезе handlers_reports (v1.64.0), где забытый патч
отправил линт сканировать прод.

⚠️ ХЕЛПЕРЫ STORAGE ИМПОРТИРУЮТСЯ ОТЛОЖЕННО, внутри функций. Импорт на уровне
модуля дал бы цикл storage ↔ journal (storage реэкспортирует имена отсюда
последней строкой файла), а отложенный вдобавок сохраняет тестам возможность
патчить имя на модуле-владельце: оно берётся из storage в момент вызова, а не
связывается при импорте.
"""

import re
from datetime import datetime
from pathlib import Path


# ─── Журнал сессий и открытые вопросы ────────────────────────────────────────
#
# ⚠️ РАНЬШЕ `_session.md` ПЕРЕЗАПИСЫВАЛСЯ ЦЕЛИКОМ, и это стоило дорого. Замер
# 2026-08-26 по аудиту (7965 вызовов, 502 сессии): открытые вопросы фиксировались
# в 68% вызовов finish_task — 948 штук, медиана 235 символов, то есть содержательные
# — и 916 из них (96%) затирались следующей сессией того же проекта. В статью
# open_questions не попадали вообще: finish_task отдаёт их только сюда. Поэтому
# сессии теперь НАКАПЛИВАЮТСЯ, а вопросы вынесены в отдельный файл со статусом,
# где живут до явного закрытия.

MAX_SESSIONS = 10          # сколько последних сессий держим в журнале
SESSION_SEP = "\n---\n\n"
MAX_NOTES = 12             # заметок по ходу на одну сессию
RUNNING_MARK = "· в работе"  # пометка незакрытого блока журнала


def _session_path(project: str, create: bool = False) -> Path:
    """Путь журнала сессий. Каталог проекта создаёт только запись (create=True):
    чтение журнала несуществующего проекта не должно заводить сам проект."""
    from memory_compiler.storage import safe_project_dir, safe_project_path
    base = safe_project_dir(project) if create else safe_project_path(project)
    return base / "_session.md"


def _split_session_blocks(text: str) -> list[str]:
    """Разбор журнала на блоки сессий, включая СТАРЫЙ однозаписный формат.

    Граница блока — ТОЛЬКО заголовок-дата «## ГГГГ-ММ-ДД», не любой «## ».
    Прежнее условие («в теле есть \\n## ») ветку старого формата не пускало
    НИКОГДА: старый файл сам состоит из разделов «## Что сделано» / «## Решения»,
    и первым блоком становилась строка «**Дата:** …» — 26 символов вместо всей
    сессии. Замер 2026-08-26 на боевой базе: 38 проектов из 41, в стартовом
    контексте вместо содержания прошлой сессии стояла одна строка с датой,
    а при первой же новой записи старый файл разъезжался на 4 псевдо-сессии
    и занимал 4 слота из MAX_SESSIONS. Держит tests/test_session_journal.py.
    """
    if not text.strip():
        return []
    body = re.sub(r"^#\s+Сесси[яи][^\n]*\n+", "", text.strip(), count=1)
    if re.search(r"(?:^|\n)## \d{4}-\d{2}-\d{2}", body):
        parts = re.split(r"\n(?=## \d{4}-\d{2}-\d{2})", body.strip())
        return [p.strip("\n").rstrip("-\n ").strip() for p in parts if p.strip()]
    # Старый формат целиком — как один блок, с датой из шапки
    m = re.search(r"\*\*Дата:\*\*\s*([0-9\- :]+)", body)
    stamp = (m.group(1).strip() if m else "ранее")
    return ["## %s\n\n%s" % (stamp, body.strip())]


def _running_notes(blocks: list[str]) -> list[str]:
    """Заметки из верхнего блока «в работе», если он СЕГОДНЯШНИЙ.

    Брошенный вчерашний блок не продолжаем: слипшиеся в один блок разные дни
    читаются как одна сессия и врут о том, на чём остановились.
    """
    if not blocks:
        return []
    head = blocks[0].splitlines()[0] if blocks[0] else ""
    if RUNNING_MARK not in head:
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    if not head.startswith("## %s" % today):
        return []
    return [l for l in blocks[0].splitlines()[1:] if l.strip()]


def running_notes_today(project: str) -> list[str]:
    """Заметки блока «в работе», которые РЕАЛЬНО вольются в ближайший итог.

    ⚠️ Существует ради того, чтобы обещание и действие шли по ОДНОМУ правилу.
    Подсказка в `finish_task` обещала вливание при любом блоке «в работе», а
    вливается только СЕГОДНЯШНИЙ (брошенный вчерашний не продолжается — v1.65.0,
    слипшиеся дни врут о том, на чём остановились). Живой случай 08.09: верхний
    блок висел «в работе» с 01.09, и подсказка обещала на него вливание, которого
    не было бы. Своя копия условия «сегодняшний ли» разъехалась бы точно так же.
    """
    path = _session_path(project)
    if not path.exists():
        return []
    return _running_notes(_split_session_blocks(path.read_text(encoding="utf-8")))


def append_note(project: str, note: str) -> Path:
    """Дописать заметку по ходу сессии в текущий блок журнала.

    Дешёвая альтернатива `save_session`, который пересобирает сводку целиком и
    потому зовётся в 10% сессий (замер 2026-08-26 по аудиту). Работа после
    последней загрузки контекста: медиана 25 минут, p90 101 — всё это время
    параллельная сессия и следующий старт не знали о происходящем.

    Git НЕ трогаем сознательно: `git add -A` по базе стоит 5.5 с, и ради одной
    строки его не платят — заметка доедет с ближайшим сохранением статьи.
    """
    note = (note or "").strip()
    if not note:
        return _session_path(project)
    path = _session_path(project, create=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    blocks = _split_session_blocks(old)
    notes = _running_notes(blocks)
    if notes:
        blocks = blocks[1:]                      # текущий блок пересоберём
    line = "- %s %s" % (datetime.now().strftime("%H:%M"), note)
    if not any(n.split(" ", 2)[-1] == note for n in notes):
        notes.append(line)
    notes = notes[-MAX_NOTES:]
    head = "## %s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M"), RUNNING_MARK)
    blocks.insert(0, "\n".join([head, ""] + notes))
    path.write_text("# Сессии: %s\n\n%s\n" % (project, SESSION_SEP.join(blocks[:MAX_SESSIONS])),
                    encoding="utf-8")
    return path


def append_session(project: str, summary: str, decisions: str = "",
                   open_questions: str = "") -> Path:
    """Дописать сессию в журнал проекта, не затирая прошлые."""
    path = _session_path(project, create=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    blocks = _split_session_blocks(old)
    # Заметки текущей сессии вливаем в её итог, а не оставляем отдельным блоком:
    # иначе одна сессия выглядит в журнале как две и занимает два слота.
    notes = _running_notes(blocks)
    if notes:
        blocks = blocks[1:]

    # ⚠️ БРОШЕННЫЕ БЛОКИ ВЛИВАЮТСЯ С ДАТОЙ (v1.75.0), а не висят вечно. Замер по
    # проду 09.09: 13 незакрытых блоков в 9 проектах, 30 заметок в них, и 10
    # журналов из 43 упёрлись в MAX_SESSIONS — у gw2 четыре слота из шести
    # занимали огрызки по одной-две заметки, у crowdsource в брошенном блоке
    # лежали 12 заметок, самое содержательное, что там было. Чистка их выбросила
    # бы, пометка ничего не меняет: слот занят, заметки в итог не доезжают.
    #
    # ⚠️ ПРАВИЛО v1.65.0 НЕ ОТМЕНЕНО. Там запрещено ПРОДОЛЖАТЬ вчерашний блок как
    # текущий: дни слипались, и «на чём остановились» врало. Здесь блок
    # закрывается, а строки переезжают в новый итог ОТДЕЛЬНОЙ секцией со своей
    # датой — читатель видит, что это хвост другого дня.
    #
    # ⚠️ ДАТА-ПОДЗАГОЛОВОК НЕ НАЧИНАЕТСЯ С «## »: `_split_session_blocks` режет
    # журнал именно по нему, и заголовок внутри блока разъехался бы на
    # псевдо-сессии — ровно регрессия v1.58.0. Держит отдельный тест.
    today = datetime.now().strftime("%Y-%m-%d")
    abandoned, kept = [], []
    for b in blocks:
        head = b.splitlines()[0] if b else ""
        if RUNNING_MARK in head and not head.startswith("## %s" % today):
            lines = [l for l in b.splitlines()[1:] if l.strip()]
            if lines:                       # пустой хвост вливать нечего
                abandoned.append((head[3:13], lines))
            continue
        kept.append(b)
    blocks = kept

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_block = ["## %s" % now, "", "**Что сделано:** %s" % (summary or "—")]
    if notes:
        new_block.append("\n**По ходу:**\n" + "\n".join(notes))
    if abandoned:
        new_block.append("\n**Из незакрытых сессий:**\n" + "\n".join(
            "*%s:*\n%s" % (day, "\n".join(lines)) for day, lines in abandoned))
    if decisions:
        new_block.append("\n**Решения:** %s" % decisions)
    if open_questions:
        new_block.append("\n**Открытые вопросы:** %s" % open_questions)
    blocks.insert(0, "\n".join(new_block).strip())

    text = "# Сессии: %s\n\n%s\n" % (project, SESSION_SEP.join(blocks[:MAX_SESSIONS]))
    path.write_text(text, encoding="utf-8")
    return path


def latest_session(project: str) -> str:
    """Последняя сессия целиком — без обрезки по символам."""
    path = _session_path(project)
    if not path.exists():
        return ""
    blocks = _split_session_blocks(path.read_text(encoding="utf-8"))
    return blocks[0] if blocks else ""


# ─── Открытые вопросы ────────────────────────────────────────────────────────


def _questions_path(project: str, create: bool = False) -> Path:
    """Путь списка вопросов; каталог проекта создаёт только запись (create=True)."""
    from memory_compiler.storage import safe_project_dir, safe_project_path
    base = safe_project_dir(project) if create else safe_project_path(project)
    return base / "_questions.md"


def _q_key(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", (text or "").lower()).strip()[:160]


def parse_questions(project: str) -> list[dict]:
    path = _questions_path(project)
    if not path.exists():
        return []
    out = []
    for block in re.split(r"\n(?=## )", path.read_text(encoding="utf-8")):
        m = re.match(r"##\s+(open|closed)\s+·\s+([0-9\- :]+)(.*)", block.strip())
        if not m:
            continue
        body = block.split("\n", 1)[1].strip() if "\n" in block else ""
        closed_at = ""
        cm = re.search(r"закрыт\s+([0-9\- :]+)", m.group(3) or "")
        if cm:
            closed_at = cm.group(1).strip()
        out.append({"status": m.group(1), "opened": m.group(2).strip(),
                    "closed": closed_at, "text": body})
    return out


def _write_questions(project: str, items: list[dict]) -> Path:
    path = _questions_path(project, create=True)
    lines = ["# Открытые вопросы: %s" % project, ""]
    for q in items:
        head = "## %s · %s" % (q["status"], q["opened"])
        if q["status"] == "closed" and q.get("closed"):
            head += " (закрыт %s)" % q["closed"]
        lines.append(head)
        lines.append("")
        lines.append(q["text"].strip())
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


# ⚠️ СПИСОК ПЛАНОВ — НЕ ВОПРОС. За одну сессию (2026-08-26) в open_questions
# попало четыре записи, и все — один и тот же перечень оставшихся работ в разных
# редакциях. Лечить это дедупом по схожести НЕ ВЫШЛО: замер по 77 парам реальных
# вопросов показал, что чистой границы нет — на Jaccard 0.49 и 0.40 сидят РАЗНЫЕ
# вопросы одного проекта (общая лексика), и порог склеивал бы их вместе с дублями.
# Отличается не схожесть, а природа записи: план перечисляет работы («Дальше по
# плану: (3)… (4)… (5)…»), вопрос описывает неясность. Признак — маркер начала
# плюс три и более перечисления; на боевой базе даёт 5 из 5 без ложных, причём
# вопросы с вариантами («Владелец решает: (1)… (2)…») остаются вопросами.
_PLAN_START = re.compile(
    r"^\s*(?:дальше\s+по\s+план|следующие\s+работы|следующие\s+шаги|не\s+сделано|"
    r"не\s+сделаны|осталось\s+из|из\s+плана|из\s+аудита|предложения\s+не\s+реализован|"
    r"ждёт\s+решения|план\s+работ|остаток\s+плана)", re.IGNORECASE)
_PLAN_ENUM = re.compile(r"\((?:\d{1,2}|[A-Za-zА-Яа-я])\)")


def is_plan_list(text: str) -> bool:
    """Перечень оставшихся работ, а не открытый вопрос."""
    t = (text or "").strip()
    return bool(_PLAN_START.match(t)) and len(_PLAN_ENUM.findall(t)) >= 3


def add_question(project: str, text: str) -> bool:
    """Добавить открытый вопрос. Дубль (тот же текст, ещё открыт) не плодим."""
    text = (text or "").strip()
    if not text or is_plan_list(text):
        return False
    items = parse_questions(project)
    key = _q_key(text)
    for q in items:
        if q["status"] == "open" and _q_key(q["text"]) == key:
            return False
    items.insert(0, {"status": "open",
                     "opened": datetime.now().strftime("%Y-%m-%d %H:%M"),
                     "closed": "", "text": text})
    _write_questions(project, items)
    return True


def close_questions(project: str, match: str, remainder: str = "") -> int:
    """Закрыть вопросы, чей текст содержит match (регистр не важен).

    `remainder` — текст ЖИВОГО остатка: закрытый вопрос уходит в историю целиком,
    а остаток заводится новым открытым вопросом.

    ⚠️ ОСТАТОК ЗАДАЁТСЯ ТЕКСТОМ, А НЕ ВЫРЕЗАЕТСЯ ЭВРИСТИКОЙ. Замер 26.08.2026:
    52% открытых вопросов (35 из 67) склеены из нескольких тем, и разрез по
    предложениям исказил бы смысл. Живой случай: в одном вопросе соседствовали
    опровергнутый барьер платформ и два действующих пункта — закрыть целиком
    значило похоронить живое, оставить как есть — транслировать опровергнутое.
    """
    needle = (match or "").strip().lower()
    if not needle:
        return 0
    items = parse_questions(project)
    n = 0
    for q in items:
        if q["status"] == "open" and needle in q["text"].lower():
            q["status"] = "closed"
            q["closed"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            n += 1
    if n:
        _write_questions(project, items)
        # остаток заводим ТОЛЬКО если что-то закрылось: иначе промах по тексту
        # породил бы вопрос-двойник рядом с нетронутым исходным
        if remainder and remainder.strip():
            add_question(project, remainder.strip())
    return n


def open_questions_list(project: str, limit: int = 0) -> list[dict]:
    items = []
    for q in parse_questions(project):
        if q["status"] != "open":
            continue
        items.append(q)
    return items[:limit] if limit else items


# ─── Reflective Memory (RMM-lite: prospective reflection on finish_task) ──────
#
# Rule-based atomic-fact extraction from session content. Inspired by Reflective
# Memory Management (arXiv 2503.08026): break a session into reusable units so
# future retrieval can hit specific facts rather than buried prose paragraphs.
# No external LLM — pattern matching on bullets, numbered lists, and Russian/English
# action verbs.

_REFLECTION_ACTION_VERBS = re.compile(
    r'\b(?:настроил|настроили|исправил|исправили|добавил|добавили|обновил|обновили|'
    r'реализовал|реализовали|решил|решили|подключил|подключили|удалил|удалили|'
    r'configured|fixed|added|updated|implemented|resolved|connected|removed|'
    r'deployed|зад\w*плои\w*|сд\w*елал\w*)\b',
    re.IGNORECASE,
)


# Negation markers that disqualify a sentence from being recorded as a fact.
_NEGATION_RE = re.compile(
    r'(?:^|\s)(?:не|never|not|n\'t|didn\'t|did not|hasn\'t|has not|haven\'t)\s',
    re.IGNORECASE,
)


def extract_reflections(content: str) -> list[str]:
    """Extract atomic facts from session content via rules.

    Sources:
      1. Top-level bullet items: '- X' or '* X'
      2. Numbered list items: '1. X'
      3. Sentences containing action verbs (настроил/fixed/added/...) — full sentence
    Sentences with negation markers ('не', 'not', "n't"…) are NOT extracted
    because they describe something that didn't happen.
    Returns deduplicated list of fact strings (trimmed).
    """
    if not content or not content.strip():
        return []

    facts: list[str] = []

    # 1+2. Bullets and numbered lists
    for line in content.splitlines():
        stripped = line.strip()
        # Bullets: - foo, * foo
        m_bullet = re.match(r'^[-*]\s+(.+)$', stripped)
        if m_bullet:
            fact = m_bullet.group(1).strip()
            if len(fact) >= 6 and not _NEGATION_RE.search(" " + fact):
                facts.append(fact)
            continue
        # Numbered: 1. foo, 2) foo
        m_num = re.match(r'^\d+[.)]\s+(.+)$', stripped)
        if m_num:
            fact = m_num.group(1).strip()
            if len(fact) >= 6 and not _NEGATION_RE.search(" " + fact):
                facts.append(fact)

    # 3. Sentences with action verbs (split content into sentences first)
    sentences = re.split(r'(?<=[.!?])\s+', content)
    for sent in sentences:
        sent = sent.strip()
        if not sent or len(sent) < 12:
            continue
        # Skip if already added as a bullet
        if any(sent in f or f in sent for f in facts):
            continue
        if _REFLECTION_ACTION_VERBS.search(sent):
            # Skip negated sentences ("не настроил", "did not configure" etc.)
            if _NEGATION_RE.search(" " + sent):
                continue
            # Cap at 200 chars
            facts.append(sent[:200].rstrip(".!? "))

    # Dedup preserving order
    seen = set()
    deduped = []
    for f in facts:
        key = f.lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped


def append_reflections(project: str, facts: list[str], cap: int = 20) -> None:
    """Append facts to <project>/_reflections.md, capping at `cap` entries (FIFO).
    No-op if facts is empty.
    """
    from memory_compiler.storage import normalize_project, project_dir

    if not facts:
        return
    proj = project_dir(project)
    refl_path = proj / "_reflections.md"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_lines = [f"- [{ts}] {f}" for f in facts]

    existing: list[str] = []
    if refl_path.exists():
        text = refl_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("- ["):
                existing.append(line)

    # Newest first, FIFO cap
    combined = new_lines[::-1] + existing  # newest entries on top
    combined = combined[:cap]

    body = (
        f"# Reflections: {normalize_project(project)}\n\n"
        f"Atomic facts extracted from sessions (FIFO {cap}, newest first).\n\n"
        + "\n".join(combined)
        + "\n"
    )
    # Atomic write: write to .tmp then rename (avoids torn writes on crash / concurrent edit)
    tmp_path = refl_path.with_suffix(refl_path.suffix + ".tmp")
    tmp_path.write_text(body, encoding="utf-8")
    tmp_path.replace(refl_path)


# ─── Чтение накопленных фактов ───────────────────────────────────────────────
#
# ⚠️ ДО v1.62.0 ЭТОТ ФАЙЛ ПИСАЛСЯ И НЕ ЧИТАЛСЯ НИКЕМ. Диагностика 2026-08-26:
# 103 КБ в 39 проектах, 632 факта, читателей в коде — ноль. Первым порывом было
# перестать писать, но выборка это опровергла: годного содержимого 96%
# («сертификат Let's Encrypt на app.dymok27.ru», «ЧекККМ_проведение: 2,2–5,0с →
# 10,47с», «тела ПЛОСКИЕ, без обёртки»), то есть механизм работал, просто выход
# был в никуда. Поэтому не удаляем, а подключаем к стартовому контексту.
#
# ⚠️ ФИЛЬТРОВАТЬ ПОЧТИ НЕЧЕГО, и жадный фильтр вреден: пробный вариант отсеивал
# по последнему символу и по кавычке в начале — и выбрасывал ценное («УЗБЕКИСТАН
# 46.8.194.10 стал полноценным узлом», описание формы в кавычках). Отсев оставлен
# минимальным: слишком короткие строки и служебные вида «proj: Имя (5)».

_REFL_JUNK = re.compile(r"^[\w-]+:\s*[^.]{0,30}\(\d+\)$")


def _reflection_lines(project: str) -> list[str]:
    from memory_compiler.storage import project_path

    try:
        path = project_path(project) / "_reflections.md"
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    out = []
    for line in text.splitlines():
        m = re.match(r"- \[[\d\- :]+\]\s+(.+)", line)
        if not m:
            continue
        fact = m.group(1).strip()
        if len(fact) < 30 or len(fact.split()) < 4 or _REFL_JUNK.match(fact):
            continue
        out.append(fact)
    return out


def relevant_reflections(project: str, topic_words: set, limit: int = 4) -> list[str]:
    """Факты прошлых сессий, пересекающиеся с темой задачи. Порядок — свежие выше.

    Без темы не отдаём ничего: вываливать в стартовый контекст двадцать фактов
    подряд — это шум, а не справка.
    """
    if not topic_words:
        return []
    scored = []
    for pos, fact in enumerate(_reflection_lines(project)):
        words = set(re.findall(r"[а-яА-ЯёЁa-zA-Z]{4,}", fact.lower()))
        overlap = len(topic_words & words)
        if overlap:
            scored.append((-overlap, pos, fact))
    scored.sort()
    return [f for _, _, f in scored[:limit]]
