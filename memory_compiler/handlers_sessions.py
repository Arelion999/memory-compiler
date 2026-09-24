"""Домен сессий: журнал, открытые вопросы, стартовый и итоговый контекст.

save_session / session_note / open_questions / close_question / load_session /
get_active_context / first_touch_context — журнал и вопросы; start_task /
finish_task — комбинированные tools старта и завершения задачи; _Block /
_render_block — сборка стартового контекста в бюджете; _journal_gap_hint —
подсказка о незакрытом дне.

Вынесено из handlers.py в v1.86.0 — пятый разрез тем же приёмом (handlers_reports
v1.64.0, journal v1.81.0, handlers_search v1.83.0, handlers_articles v1.84.0). Шов
выбран замером связности (транзитивное замыкание): домен тянет наружу общие
хелперы ядра, и никто из стейеров не тянет обратно ничего из домена.

⚠️ КРОСС-МОДУЛЬНЫЕ ИМЕНА — ОТЛОЖЕННО ИЗ handlers, внутри функций: _whoosh_async,
_weighted_budgets, _cut_section_body и START_BLOCK_FLOOR остаются в ядре
(_whoosh_async нужен route_project; _weighted_budgets с дефолтом START_BLOCK_FLOOR
с v1.87.0 нужен лишь start_task — бюджет превью search, где он был общим, удалён;
_cut_section_body — handlers_articles); _rerank_async/stale_summary/save_lesson
реэкспортированы handlers из других детей. Импорт на уровне модуля дал бы цикл
handlers ↔ handlers_sessions, а отложенный вдобавок СОХРАНЯЕТ ТЕСТАМ ПАТЧ на
handlers: test_reflections_read/test_stale_precision патчат handlers._whoosh_async
и зовут handlers.start_task — имя обязано читаться из handlers в момент вызова.
handlers реэкспортирует все вынесенные имена — tools.py и тесты ходят через
handlers.<имя> как прежде.

⚠️ МОДУЛЬ ДЕРЖИТ СВОЙ KNOWLEDGE_DIR — tests/conftest.py патчит его отдельно
(5-й случай класса после reports/search/articles/maintenance); без этого журнал
и стартовый контекст молча уйдут в БОЕВУЮ базу мимо tmp_path. PROJECTS модулю не
нужен: open_questions читает его как memory_compiler.config.PROJECTS живьём.
"""

import asyncio
import re
from datetime import datetime

from mcp.types import TextContent

from memory_compiler.config import KNOWLEDGE_DIR, track_access
from memory_compiler.storage import (
    append_session, append_note, latest_session, RUNNING_MARK, running_notes_today,
    add_question, close_questions, open_questions_list, project_corrections,
    relevant_reflections, read_project_deps, git_commit,
    extract_reflections, append_reflections, safe_project_path,
)


# ─── Session Handoff ─────────────────────────────────────────────────────────


async def save_session(project: str, summary: str, decisions: str = "", open_questions: str = "") -> list[TextContent]:
    """Дописать сессию в журнал проекта; открытые вопросы — в накопительный список.

    ⚠️ Раньше файл сессии ПЕРЕЗАПИСЫВАЛСЯ целиком, и открытые вопросы жили лишь
    до следующей сессии того же проекта. Замер 2026-08-26 по аудиту (7965 вызовов,
    502 сессии): вопросы фиксировались в 68% вызовов finish_task — 948 штук,
    медиана 235 символов — и 916 из них (96%) затирались. В статью они не
    попадают вообще. Теперь сессии накапливаются (последние MAX_SESSIONS), а
    вопросы ведутся отдельным файлом со статусом и закрываются явно.
    """
    await asyncio.to_thread(append_session, project, summary, decisions, open_questions)
    added = await asyncio.to_thread(add_question, project, open_questions) if open_questions else False
    await asyncio.to_thread(git_commit, f"session: {project}")
    msg = f"✅ Контекст сессии сохранён: {project}/_session.md"
    if added:
        n = len(open_questions_list(project))
        msg += f"\n❓ Открытый вопрос добавлен в {project}/_questions.md (всего открытых: {n})"
    return [TextContent(type="text", text=msg)]


# ── Контекст без спроса при первом обращении к проекту (v1.68.0) ────────────
# Замер 26.08.2026 по аудиту (109 сессий за месяц): 81% начинаются НЕ с загрузки
# контекста — первым вызовом идут save_lesson, edit_article, search, finish_task.
# Из этих «слепых» сессий 85 пишут в базу, то есть работают всерьёз, не прочитав,
# на чём остановились. У 86 из 89 по проекту было что показать; частота подсказки
# выходит около 3 раз в сутки — это не шум.
FIRST_TOUCH_CHARS = 900        # потолок подсказки: это указатель, а не контекст


FIRST_TOUCH_QUESTIONS = 2


def first_touch_context(project: str) -> str:
    """Короткая подсказка о состоянии проекта: открытые вопросы и незакрытая работа.

    ⚠️ ПОВОД — ТОЛЬКО НЕЗАКРЫТОЕ. Обычная прошлая сессия поводом не служит: это
    рядовая история проекта, за ней ходят в `start_task`, и дёргать ею на каждом
    первом вызове значит превратить подсказку в фон.
    """
    from memory_compiler.handlers import _cut_section_body  # ядро: общий с _render_block
    parts: list[str] = []
    try:
        pending = open_questions_list(project, limit=FIRST_TOUCH_QUESTIONS)
    except (ValueError, OSError):
        pending = []
    if pending:
        # ⚠️ Предупреждение о поправках идёт ПЕРЕД вопросами, а не после: подсказка
        # ограничена бюджетом и режется с конца — сигнал, что вопросы могут быть
        # опровергнуты, важнее второго вопроса в списке.
        try:
            corr = project_corrections(project)
        except Exception:
            corr = []
        if corr:
            parts.append("⚠️ В проекте есть поправки ("
                         + "; ".join(t or f for f, t in corr[:2])
                         + ") — вопросы ниже могли быть ими отменены.")
        parts.append("Открытые вопросы:")
        for q in pending:
            parts.append(f"- {q['text']}")
    try:
        session_text = latest_session(project) or ""
    except OSError:
        session_text = ""
    head = session_text.splitlines()[0] if session_text else ""
    if RUNNING_MARK in head:
        body = "\n".join(session_text.splitlines()[1:]).strip()
        if body:
            parts.append(f"Сессия по проекту не закрыта:\n{body}")
    if not parts:
        return ""
    body = "\n".join(parts)
    tail = f"\n\nПолный контекст — `start_task(project=\"{project}\")`."
    room = FIRST_TOUCH_CHARS - len(tail) - 80
    if len(body) > room:
        body, _ = _cut_section_body(body, room)
    return (f"\n\n📌 **Первое обращение к `{project}` в этой сессии.** "
            f"Контекст не загружался:\n{body}{tail}")


async def session_note(note: str, project: str) -> list[TextContent]:
    """Заметка по ходу сессии: одна строка в текущий блок журнала.

    Дополнять контекст ПОСРЕДИ работы было нечем: `save_session` пересобирает
    сводку целиком и зовётся в 10% сессий (замер 2026-08-26 по аудиту, 502
    сессии). При этом работа после последней загрузки контекста — медиана 25
    минут, p90 101: всё найденное в эти минуты для параллельной сессии и для
    следующего старта не существовало.

    Дёшево по построению: без git-коммита (5.5 с на `git add -A`), без
    пересборки сводки, без индексации — файл журнала служебный и в поиск не
    попадает (search.SERVICE_FILES).
    """
    await asyncio.to_thread(append_note, project, note)
    return [TextContent(type="text", text=f"✅ Заметка записана в {project}/_session.md")]


async def open_questions(project: str = "all") -> list[TextContent]:
    """Незакрытые вопросы — то, на чём останавливались в прошлых сессиях."""
    import memory_compiler.config as _cfg
    projects = [project] if project != "all" else [p for p in _cfg.PROJECTS if p != "daily"]
    parts, total = [], 0
    for proj in projects:
        try:
            items = await asyncio.to_thread(open_questions_list, proj)
        except ValueError:
            continue
        if not items:
            continue
        total += len(items)
        parts.append(f"\n## {proj} ({len(items)})")
        for q in items[:10]:
            age = ""
            try:
                d = datetime.strptime(q["opened"][:10], "%Y-%m-%d")
                days = (datetime.now() - d).days
                age = f" · {days} дн назад" if days else " · сегодня"
            except ValueError:
                pass
            parts.append(f"- **{q['opened']}**{age}\n  {q['text'][:400]}")
        # Поправки проекта — рядом с вопросами. Какой именно вопрос отменён,
        # машина не угадывает (проверено и отвергнуто, см. storage), но знать о
        # существовании поправки читающий обязан: вопрос мог быть заведён раньше.
        corr = await asyncio.to_thread(project_corrections, proj)
        if corr:
            parts.append(f"\n⚠️ В проекте есть поправки — проверьте, не отменяют ли они вопросы выше:")
            for fname, title in corr[:3]:
                parts.append(f"  - {title or fname} ({fname})")
    if not total:
        where = "во всех проектах" if project == "all" else f"в {project}"
        return [TextContent(type="text", text=f"Открытых вопросов {where} нет.")]
    tail = "\n\n*Решённый закрывать через `close_question(project, match)` — по куску текста.*"
    return [TextContent(type="text", text=f"# Открытые вопросы ({total})" + "\n".join(parts) + tail)]


async def close_question(project: str, match: str, remainder: str = "") -> list[TextContent]:
    """Закрыть вопрос(ы), чей текст содержит match; `remainder` — живой остаток.

    Половина открытых вопросов (35 из 67 по замеру 26.08.2026) склеена из
    нескольких тем: закрыть целиком значит похоронить живые пункты, оставить —
    транслировать уже решённое. Остаток задаётся ТЕКСТОМ, а не вырезается
    эвристикой: разрез по предложениям исказил бы смысл.
    """
    n = await asyncio.to_thread(close_questions, project, match, remainder)
    if not n:
        return [TextContent(type="text", text=f"⚠️ В {project} не найдено открытых вопросов по «{match}».")]
    await asyncio.to_thread(git_commit, f"questions: close in {project}")
    left = len(open_questions_list(project))
    msg = f"✅ Закрыто вопросов: {n}. Осталось открытых в {project}: {left}"
    if remainder and remainder.strip():
        msg += f"\n↩️ Живой остаток заведён отдельным вопросом: {remainder.strip()[:120]}"
    return [TextContent(type="text", text=msg)]


async def load_session(project: str) -> list[TextContent]:
    session_path = safe_project_path(project) / "_session.md"
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
        parts.append(f"\n\u26a0\ufe0f {stale_count} статей в {project} не обновлялись >90 дней. Запусти `lint` для деталей.")

    return [TextContent(type="text", text="\n".join(parts))]


# ─── get_active_context ──────────────────────────────────────────────────────


async def get_active_context(project: str) -> list[TextContent]:
    ctx_path = safe_project_path(project) / "_active_context.md"
    if not ctx_path.exists():
        return [TextContent(type="text", text=f"Нет активного контекста для {project}.")]
    text = ctx_path.read_text(encoding="utf-8")
    return [TextContent(type="text", text=text)]


# ─── Комбинированные tools (start/finish task) ──────────────────────────────


# Бюджет стартового контекста. Прежние 400/600 символов резали последнюю
# сессию на полуслове; блок журнала берётся целиком, а лимит служит потолком.
SESSION_CHARS = 1800


SESSION_MAX_QUESTIONS = 5


SESSION_Q_CHARS = 300


# ── Общий бюджет стартового контекста (v1.65.0) ─────────────────────────────
# До этого у каждого блока был свой срез в символах, и лимиты не знали друг о
# друге. Замер 2026-08-26 на боевой базе: 50% показанных открытых вопросов
# резались по 300 символов, ещё 12 не показывались вовсе — а у 28 проектов из 46
# весь стартовый контекст не дотягивал и до 1500 символов, то есть место было.
# Размер ответа при этом гулял от 457 до 6808: общего потолка не существовало,
# он складывался стихийно из суммы независимых срезов.
# ⚠️ ПОТОЛОК СНИЖЕН 6000 → 4500 (v1.89.0). Замер 20.09.2026 по транскриптам,
# 164 вызова за 7 дней (871 тыс. символов): медиана 5362, у потолка — половина
# вызовов. Состав: «Найдено» 34,4%, открытые вопросы 23,2%, сессия с шапкой
# 20,5%, связанные действия 11,5%, факты 6,8%, runbooks 2,3%, сроки 1,2%.
# ⚠️ ВЫКИНУТЬ БЛОК РАДИ ЭКОНОМИИ БЕССМЫСЛЕННО: раздача water-fill'ом отдаёт
# освободившееся место голодным соседям, а голодны они у половины вызовов —
# размер ответа держит ТОЛЬКО это число. Кого резать, решают веса блоков:
# вопросы и сессия (3.0), находки (2.5), сроки (2.0) уцелевают, а runbooks
# (0.5), compact (0.8), зависимые проекты (1.0) и решения (1.2) ужимаются.
# ⚠️ 4500 → 4000 (v1.91.0): чистка повторов, оценок и длинных строк снимает около 11%
# выдачи, и потолок снижен ровно на столько — те же сведения короче. Без снижения
# water-fill отдал бы освободившееся место соседям, и размер не изменился бы.
START_BUDGET = 4000        # потолок на обрезаемые блоки, символов


class _Block:
    """Кусок стартового контекста: заголовок, пункты и приоритет.

    Пункты — целые смысловые единицы (вопрос, находка, факт). Внутри бюджета
    они набираются ЦЕЛИКОМ, пока влезают: половина вопроса хуже, чем вопрос и
    честная пометка «ещё 3».
    """

    __slots__ = ("key", "header", "items", "weight", "sep")

    def __init__(self, key: str, header: str, items: list[str], weight: float,
                 sep: str = "\n"):
        self.key = key
        self.header = header
        self.items = [i for i in items if i and i.strip()]
        self.weight = weight
        self.sep = sep

    @property
    def want(self) -> int:
        if not self.items:
            return 0
        return len(self.header) + sum(len(i) + len(self.sep) for i in self.items)


def _render_block(block: "_Block", budget: int) -> str:
    """Собрать блок в пределах бюджета: целые пункты, пока влезают; последний
    подрезается по границе строки; не поместившиеся — считаются вслух."""
    from memory_compiler.handlers import _cut_section_body, START_BLOCK_FLOOR  # ядро
    if not block.items or budget <= 0:
        return ""
    left = budget - len(block.header)
    if left <= 0:
        return ""
    shown, cut_tail = [], False
    for item in block.items:
        need = len(item) + len(block.sep)
        if need <= left:
            shown.append(item)
            left -= need
            continue
        # последний влезающий подрезаем, только если от него остаётся смысл
        if not shown and left > START_BLOCK_FLOOR:
            piece, _ = _cut_section_body(item, left)
            shown.append(piece)
            left = 0
            cut_tail = True
        break
    if not shown:
        return ""
    hidden = len(block.items) - len(shown) - (1 if cut_tail else 0)
    parts = [block.header, *shown]
    if hidden > 0:
        parts.append(f"*…ещё {hidden} — спроси `open_questions` / `search`*"
                     if block.key == "questions" else f"*…ещё {hidden}*")
    return block.sep.join(parts)


# ── Статья показывается один раз (v1.91.0) ──────────────────────────────────
# Замер 24.09.2026: в 29% выдач одна статья стояла сразу в «Найдено»,
# «Связанных действиях» и «Решениях по теме». Блок пропускает уже показанное:
# по паре (проект, файл), а у строк активности — по заголовку, файла в них нет.
def _title_key(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


_ACTIVITY_TITLE_RE = re.compile(r"\*\*(.+?)\*\*")


def _activity_title(line: str) -> str:
    m = _ACTIVITY_TITLE_RE.search(line)
    return _title_key(m.group(1)) if m else ""


# Строка отрывка у статьи без переносов — целый абзац: p90 335, максимум 582
# символа (замер @link28rus 24.09.2026). Отрывок — указатель, а не пересказ.
PREVIEW_LINE_MAX = 240


def _clip_line(line: str) -> str:
    return line if len(line) <= PREVIEW_LINE_MAX else line[:PREVIEW_LINE_MAX] + "…"


async def start_task(topic: str, project: str = "all") -> list[TextContent]:
    """Начать задачу: hybrid retrieval (BM25+semantic) + cross-encoder rerank + filter by relevance.

    Continuation intent: if topic is a generic "continue" phrase (mostly stopwords),
    skip semantic search entirely — load active context + last session for the project.
    Industry pattern: continuation is session restoration, not RAG.

    Объём выдачи держит ОДИН бюджет (START_BUDGET), а не срез у каждого блока:
    блоки заявляют желаемую длину и приоритет, `_weighted_budgets` раздаёт
    water-fill'ом. Прежние независимые лимиты (сессия 1800, вопрос 300, факт 220,
    compact 600, решение 100) вели себя ровно наоборот нужному — резали там, где
    место было (28 проектов из 46 не выбирали и 1500 символов), и не давали
    потолка там, где выдача разрасталась (457..6808 символов на вызов).
    """
    from memory_compiler.search import is_low_confidence_query
    # ядро/другие дети — отложенно из handlers, чтобы патчи тестов на handlers.<имя>
    # (test_reflections_read/test_stale_precision патчат handlers._whoosh_async) читались:
    from memory_compiler.handlers import (
        _whoosh_async, _rerank_async, _weighted_budgets, stale_summary,
    )
    MIN_SCORE = 15  # min hybrid score
    MIN_RERANK = 0.0  # cross-encoder score threshold (BAAI/bge-reranker-base outputs ~[-10, 10])
    parts = []
    blocks: list[_Block] = []
    shown_files: set[tuple[str, str]] = set()
    shown_titles: set[str] = set()

    # Topic words for relevance checks
    topic_words = {w.lower() for w in re.split(r'[\s\-_,.:;]+', topic) if len(w) > 3}

    # Continuation intent — skip RAG, go straight to session restoration
    is_continuation = is_low_confidence_query(topic)

    parts.append(f"# Контекст для: {topic}\n")

    if is_continuation:
        parts.append("*Запрос распознан как «продолжить работу» — показываю недавнюю активность по проекту.*\n")
        relevant = []
    else:
        # 1. Hybrid retrieval — берём top-20, ререйнкер выбирает top-3
        candidates = await _whoosh_async(topic, project=project, limit=20)
        candidates = [r for r in candidates if r.get("score", 0) >= MIN_SCORE]
        reranked = await _rerank_async(topic, candidates, top_k=5)
        # Final filter by rerank_score (reranker may say all are weak)
        relevant = [r for r in reranked if r.get("rerank_score", 1.0) >= MIN_RERANK]
        if not relevant and reranked:
            relevant = reranked[:1]  # at least show top-1 even if low

        if relevant:
            track_access([f"{r['project']}/{r['file']}" for r in relevant])
            found_items = []
            for r in relevant[:3]:
                lines = r["preview"].splitlines()
                # Первая строка превью — заголовок этой же статьи («# …»), а он уже
                # стоит в «### [проект] Заголовок»: берём вместо него строку тела.
                if lines and lines[0].lstrip("# ").strip() == r["title"].strip():
                    lines = lines[1:]
                preview = "\n".join(_clip_line(line) for line in lines[:4])
                # Оценки в заголовке нет: между запросами она не откалибрована, а
                # порядок находок и так идёт по ней.
                found_items.append(f"### [{r['project']}] {r['title']}\n{preview}")
                shown_files.add((r["project"], r["file"]))
                shown_titles.add(_title_key(r["title"]))
            # ⚠️ Заголовок называет то, что РЕАЛЬНО отбирало: реранкер выключен
            # с v1.27.0, `rerank_score` при этом не проставляется вовсе, и
            # фильтр MIN_RERANK ниже пропускает всё по дефолту 1.0. Обещание
            # «hybrid+rerank» на выключенном реранкере — враньё модели о том,
            # чем отобраны находки.
            from memory_compiler.handlers_search import RERANK_ENABLED
            how = "hybrid+rerank" if RERANK_ENABLED else "hybrid"
            blocks.append(_Block("found", f"## Найдено ({len(relevant)} релевантных, {how})",
                                 found_items, weight=2.5, sep="\n\n"))
        else:
            parts.append("*Похожих кейсов не найдено в базе.*\n")

    # 2. Determine target project
    target_project = project if project != "all" else (relevant[0]["project"] if relevant else "general")

    # 3. Active context — на continuation показываем всё, иначе фильтр по topic_words
    ctx_path = KNOWLEDGE_DIR / target_project / "_active_context.md"
    if ctx_path.exists():
        ctx_text = ctx_path.read_text(encoding="utf-8")
        if is_continuation:
            # Continuation intent → recent activity wholesale (top 5)
            ctx_lines = [l for l in ctx_text.splitlines() if l.startswith("- [")]
            blocks.append(_Block("activity", f"## Недавняя активность в {target_project}",
                                 ctx_lines[:5], weight=1.5))
        elif topic_words:
            relevant_lines = []
            for line in ctx_text.splitlines():
                if not line.startswith("- ["):
                    continue
                line_words = set(re.findall(r'[а-яА-ЯёЁa-zA-Z]{4,}', line.lower()))
                if not (topic_words & line_words):
                    continue
                title = _activity_title(line)
                if title and title in shown_titles:
                    continue
                relevant_lines.append(line)
                if len(relevant_lines) == 3:
                    break
            for line in relevant_lines:
                if _activity_title(line):
                    shown_titles.add(_activity_title(line))
            blocks.append(_Block("activity", f"## Связанные действия в {target_project}",
                                 relevant_lines, weight=1.5))

    # 3-. Факты прошлых сессий по теме. Файл `_reflections.md` до v1.62.0 писался
    # на каждом finish_task и не читался НИКЕМ — 103 КБ в 39 проектах впустую.
    # Отдаём только пересекающиеся с темой и не больше четырёх: это справка,
    # а не второй поиск.
    try:
        facts = relevant_reflections(target_project, topic_words, limit=4)
    except Exception:
        facts = []
    blocks.append(_Block("facts", f"## Факты прошлых сессий ({target_project})",
                         [f"- {f}" for f in facts], weight=1.5))

    # 3a. Сроки на исходе. Инструмент stale_facts за 4.5 месяца не позвали НИ РАЗУ
    # (замер по аудиту): проверка, которую надо вспомнить и вызвать, механизмом
    # актуальности не работает. Показываем сам, по проекту, и только когда есть
    # что сказать — через кэш, иначе скан добавил бы к старту 400-650 мс.
    try:
        deadlines = await asyncio.to_thread(stale_summary, target_project, 30, 3)
    except Exception:
        deadlines = []                       # сроки не должны ронять старт задачи
    # ⚠️ ИСТЁКШЕЕ В СТАРТОВЫЙ КОНТЕКСТ НЕ ПОПАДАЕТ (v1.89.0): после даты
    # предупреждать не о чем, а повторяться такой пункт может неделями. Живой
    # случай — «554 стартмани сгорают 09.09.2026 — истёк»: 11 дней подряд, 50
    # выдач из 164 за неделю, и всё это время факт был уже неправдой. В отчёте
    # `stale_facts` истёкшее остаётся: его спрашивают явно и смотрят как
    # историю за 90 дней.
    dl_items = []
    for d in deadlines:
        if d["days_left"] < 0:
            continue
        dl_items.append(f"- **{d['title'][:90]}** — {d['date']}, "
                        f"осталось {d['days_left']} дн")
    blocks.append(_Block("deadlines", f"## Сроки на исходе ({target_project})",
                         dl_items, weight=2.0))

    # 3b. Открытые вопросы проекта — то, на чём остановились и не закрыли.
    # Показываем ВСЕГДА, когда они есть: до v1.58.0 96% зафиксированных вопросов
    # затирались следующей сессией и до следующего старта не доезжали вовсе.
    # Длину вопроса держит бюджет, а не срез по 300 символов: замер 2026-08-26 —
    # так обрезалась ПОЛОВИНА показанных вопросов (18 из 36).
    try:
        pending_q = open_questions_list(target_project, limit=SESSION_MAX_QUESTIONS)
    except ValueError:
        pending_q = []
    q_items = [f"- **{q['opened']}** — {q['text']}" for q in pending_q]
    if q_items:
        corr = await asyncio.to_thread(project_corrections, target_project)
        if corr:
            names = "; ".join(t or f for f, t in corr[:2])
            # в начало списка: блок режется бюджетом с конца
            q_items.insert(0, f"⚠️ В проекте есть поправки ({names}) — вопросы "
                              f"ниже могли быть ими отменены.")
    blocks.append(_Block("questions", f"## Открытые вопросы ({target_project})",
                         q_items, weight=3.0))

    # 4. Session — на continuation показываем всегда, иначе фильтр по словам.
    # Берём ПОСЛЕДНИЙ БЛОК ЖУРНАЛА целиком, а не срез файла по символам: файл
    # накопительный, и срез отдавал бы свежую сессию вперемешку со старыми,
    # обрываясь на полуслове.
    session_text = latest_session(target_project)
    if session_text:
        # Незакрытая сессия — это «что происходит прямо сейчас», её показываем
        # ВСЕГДА и первым делом: заметку писали именно затем, чтобы её увидели,
        # в том числе параллельная сессия с другой темой.
        running = RUNNING_MARK in session_text.splitlines()[0]
        show_session = running or is_continuation
        if not show_session and topic_words:
            session_words = set(re.findall(r'[а-яА-ЯёЁa-zA-Z]{4,}', session_text.lower()))
            show_session = bool(topic_words & session_words)
        if show_session:
            header = (f"## Сессия в работе ({target_project}) — не закрыта" if running
                      else f"## Предыдущая сессия ({target_project})")
            blocks.append(_Block("session", header, [session_text], weight=3.0))

    # 4b. Compact history — резюме сжатий контекста (новое в v1.4.0)
    # Continuous memory через compact-границы. Показываем только при continuation
    # или явных topic_words (не засорять обычный поиск).
    compact_path = KNOWLEDGE_DIR / target_project / "_compact_history.md"
    if compact_path.exists() and (is_continuation or topic_words):
        compact_text = compact_path.read_text(encoding="utf-8")
        # Парсим первый ## блок (самый свежий)
        cblocks = re.split(r"^## ", compact_text, flags=re.MULTILINE)
        recent_block = cblocks[1].strip() if len(cblocks) > 1 else ""
        blocks.append(_Block("compact",
                             f"## Compact history ({target_project}) — последний сжатый контекст",
                             [f"## {recent_block}"] if recent_block else [], weight=0.8))

    # 5. Search in dependent projects (только релевантные)
    deps = read_project_deps(target_project)
    if deps:
        dep_results = []
        for dep in deps:
            dr = await _whoosh_async(topic, project=dep, limit=2)
            dep_results.extend([r for r in dr if r.get("score", 0) >= MIN_SCORE])
        dep_results = [r for r in dep_results if (r["project"], r["file"]) not in shown_files]
        if dep_results:
            dep_results.sort(key=lambda r: -r.get("score", 0))
            dep_items = []
            for r in dep_results[:2]:
                preview = "\n".join(_clip_line(line) for line in r["preview"].splitlines()[:3])
                dep_items.append(f"### [{r['project']}] {r['title']}\n{preview}")
                shown_files.add((r["project"], r["file"]))
                shown_titles.add(_title_key(r["title"]))
            blocks.append(_Block("deps", f"## Из зависимых проектов ({', '.join(deps)})",
                                 dep_items, weight=1.0, sep="\n\n"))

    # 5. Relevant decisions (brief, only high-score)
    # ⚠️ ПОМЕЧАЕМ ПОКАЗАННЫМИ ТОЛЬКО ПЕРВЫЕ ТРИ (не каждое прошедшее фильтр):
    # ниже в блоки уходит decisions_found[:3], а раньше shown_files/shown_titles
    # пополнялись для ВСЕХ прошедших — четвёртое и далее решение, реально не
    # попавшее в вывод, всё равно гасило совпадающую находку/runbook дальше по
    # функции.
    decision_results = await _whoosh_async(topic, project=target_project, limit=10)
    decisions_candidates = []
    local_seen_files: set[tuple[str, str]] = set()
    local_seen_titles: set[str] = set()
    for r in decision_results:
        if r.get("score", 0) < 30:
            continue
        key_file = (r["project"], r["file"])
        key_title = _title_key(r["title"])
        if key_file in shown_files or key_title in shown_titles:
            continue
        if key_file in local_seen_files or key_title in local_seen_titles:
            continue
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        if r["file"].startswith("decision_") or "**Тип:** decision" in fpath.read_text(encoding="utf-8")[:500]:
            # Extract first line of decision section
            text = fpath.read_text(encoding="utf-8")
            decision_line = ""
            for line in text.splitlines():
                if line.startswith("## Решение"):
                    idx = text.splitlines().index(line)
                    if idx + 1 < len(text.splitlines()):
                        decision_line = text.splitlines()[idx + 1].strip()
                    break
            decisions_candidates.append((f"- **{r['title']}** — {decision_line[:100]}",
                                          key_file, key_title))
            local_seen_files.add(key_file)
            local_seen_titles.add(key_title)
    decisions_found = [c[0] for c in decisions_candidates[:3]]
    for _, key_file, key_title in decisions_candidates[:3]:
        shown_files.add(key_file)
        shown_titles.add(key_title)
    blocks.append(_Block("decisions", "## Решения по теме", decisions_found, weight=1.2))

    # 6. Relevant runbooks (brief, only matching)
    proj_path = KNOWLEDGE_DIR / target_project
    runbooks_found = []
    if proj_path.exists():
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            head = md.read_text(encoding="utf-8")[:300]
            if "**Тип:** runbook" not in head:
                continue
            title = head.splitlines()[0].lstrip("# ").strip() if head.splitlines() else md.stem
            # Check relevance: any topic word in title
            title_words = {w.lower() for w in topic.split() if len(w) > 3}
            if title_words & {w.lower() for w in title.split()}:
                total = head.count("- [ ]") + head.count("- [x]")
                runbooks_found.append(f"- **{title}** ({md.name}, {total} шагов)")
    blocks.append(_Block("runbooks", "## Runbooks", runbooks_found[:3], weight=0.5))

    # 7. Раздача общего бюджета: короткий блок берёт своё целиком, неиспользованное
    # достаётся тем, кому не хватило, приоритет решает, кого резать первым.
    live = [b for b in blocks if b.items]
    budgets = _weighted_budgets([b.want for b in live], [b.weight for b in live], START_BUDGET)
    for b, bud in zip(live, budgets):
        rendered = _render_block(b, bud)
        if rendered:
            parts.append("\n" + rendered + "\n")

    parts.append("\n---\n*Приступай к задаче. По завершении вызови `finish_task`.*")
    return [TextContent(type="text", text="\n".join(parts))]


# ── Журнал без сводки: молчать об этом нельзя (v1.72.0) ─────────────────────
# Вопрос, оставшийся от v1.71.1: вопрос стал доезжать без сводки, а САМА сессия
# в журнал по-прежнему не попадала. Живой случай 27.08.2026 — блок за день
# появился только ручным `save_session`, и до того старт следующей сессии
# показывал бы вчерашний день. Модель узнать об этом неоткуда: ответ выглядел
# успешным.
#
# ⚠️ ПОДСКАЗКА НЕ БЕЗУСЛОВНАЯ. `finish_task` зовут по нескольку раз за сессию, и
# напоминание на каждом стало бы фоном — ровно та болезнь, из-за которой
# подсказка о `session_note` сделана раз в окно. Молчим, когда сегодняшний блок
# уже закрыт сводкой; блок «в работе» закрытым не считается — заметки вольются
# в итог только вместе со сводкой, иначе сессия навсегда останется незакрытой.
def _journal_gap_hint(project: str) -> str:
    block = latest_session(project)
    head = block.splitlines()[0] if block else ""
    today = datetime.now().strftime("%Y-%m-%d")
    if head.startswith("## %s" % today) and RUNNING_MARK not in head:
        return ""
    # ⚠️ ОБЕЩАЕМ ВЛИВАНИЕ ПО ТОМУ ЖЕ ПРАВИЛУ, ПО КОТОРОМУ ОНО ПРОИСХОДИТ. Раньше
    # условием был сам факт RUNNING_MARK, а вливается только СЕГОДНЯШНИЙ блок:
    # брошенный вчерашний намеренно не продолжается (v1.65.0). Обещание,
    # выданное по своей копии условия, — тот же класс, что чинили весь день.
    notes = running_notes_today(project)
    tail = (" Заметки по ходу (%d) вольются в итоговый блок." % len(notes)) if notes else ""
    return ("📓 Сводки сессии не было — день не попал в журнал проекта, и на старте "
            "следующей сессии его не будет видно. Допиши: "
            "save_session(project=\"%s\", summary=…).%s" % (project, tail))


async def finish_task(topic: str, content: str, project: str, tags: list = None,
                      session_summary: str = "", open_questions: str = "",
                      triggers: list = None) -> list[TextContent]:
    """Завершить задачу: save_lesson + save_session. Один вызов вместо двух."""
    from memory_compiler.handlers import save_lesson  # реэкспорт из handlers_articles
    parts = []

    # 1. Сохранить урок
    lesson_result = await save_lesson(topic, content, project, tags, triggers=triggers)
    parts.append(lesson_result[0].text)

    # 2. Сохранить сессию
    # ⚠️ ВОПРОС НЕ ПРИВЯЗАН К СВОДКЕ (v1.71.1). Раньше save_session звался только
    # под `if session_summary:`, и вместе с сессией МОЛЧА терялся open_questions:
    # вызов без сводки писал одну статью-урок, вопрос не заводился, а ответ
    # отчитывался успехом. Живой случай 27.08.2026 — 595 символов с цифрами
    # контрольного замера не легли никуда, и старт следующей сессии показывал
    # позавчерашний вопрос. Сама ветка остаётся: append_session ВСЕГДА вставляет
    # новый блок, поэтому вызов без сводки журнал не пополняет — иначе одна
    # сессия заняла бы два слота из MAX_SESSIONS.
    if session_summary:
        session_result = await save_session(project, session_summary, "", open_questions or "")
        parts.append(session_result[0].text)
    elif open_questions:
        if await asyncio.to_thread(add_question, project, open_questions):
            n = len(open_questions_list(project))
            parts.append(f"❓ Открытый вопрос добавлен в {project}/_questions.md (всего открытых: {n})")
    if not session_summary:
        hint = _journal_gap_hint(project)
        if hint:
            parts.append(hint)

    # 3. Prospective reflection — извлечь atomic facts из content + session_summary
    reflections = extract_reflections(content + "\n" + (session_summary or ""))
    if reflections:
        append_reflections(project, reflections)
        parts.append(f"\U0001f9e0 Reflections: +{len(reflections)} atomic facts")

    parts.append("\n*Задача записана в базу знаний.*")
    return [TextContent(type="text", text="\n".join(parts))]
