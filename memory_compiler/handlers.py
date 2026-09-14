"""
Tool handler implementations for memory-compiler MCP server.
All async functions return list[TextContent].
"""
import asyncio
import re
import shutil
from datetime import datetime, timedelta, date
from typing import Optional

import numpy as np
from mcp.types import TextContent

from memory_compiler.config import (
    KNOWLEDGE_DIR, track_access, article_meta, save_article_meta,
    _discover_projects,
)
from memory_compiler.search import (
    whoosh_search, rebuild_index, rebuild_embeddings,
)
# Модульный импорт: _embeddings/_embed_texts переприсваиваются в rebuild_embeddings
# (свап нового dict). Импорт `from search import _embeddings` заморозил бы ССЫЛКУ на
# старый объект — delete/remove чистили бы устаревший dict, а semantic-поиск ходил по
# новому → удалённая статья оставалась бы фантомом. Обращаемся через модуль.
import memory_compiler.search as _search
from memory_compiler.storage import (
    parse_meta_value, _parse_frontmatter,
    regenerate_index, git_commit,
    append_session, append_note, latest_session, RUNNING_MARK, running_notes_today,
    add_question, close_questions, open_questions_list,
    project_corrections,
    relevant_reflections,
    format_git_refs,
    read_project_deps, write_project_deps,
    log_event,
    extract_reflections, append_reflections,
    safe_project_dir,
    strip_code_blocks,
)











async def _whoosh_async(query: str, project: str = "all", limit: int = 10) -> list[dict]:
    """whoosh_search в потоке: он CPU-тяжёлый (semantic dot-product по всем эмбеддингам +
    при холодном старте ленивая загрузка embed-модели). На event loop он замораживал весь
    сервер (/api/health, параллельные запросы). Общий помощник для всех async-хендлеров."""
    return await asyncio.to_thread(whoosh_search, query, project=project, limit=limit)















# ─── lint ────────────────────────────────────────────────────────────────────






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
    session_path = safe_project_dir(project) / "_session.md"
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


# ─── get_summary ─────────────────────────────────────────────────────────────












# ─── get_active_context ──────────────────────────────────────────────────────


async def get_active_context(project: str) -> list[TextContent]:
    ctx_path = safe_project_dir(project) / "_active_context.md"
    if not ctx_path.exists():
        return [TextContent(type="text", text=f"Нет активного контекста для {project}.")]
    text = ctx_path.read_text(encoding="utf-8")
    return [TextContent(type="text", text=text)]












def _cut_section_body(body: str, budget: int) -> tuple[str, bool]:
    """Обрезать тело секции до бюджета по границе строки (если она не слишком рано),
    пометив срез многоточием. Влезает целиком → как есть."""
    if len(body) <= budget:
        return body, False
    head = body[:budget]
    nl = head.rfind("\n")
    if nl > budget * 0.6:
        head = head[:nl]
    return head + "\n…", True






























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
START_BUDGET = 6000        # потолок на обрезаемые блоки, символов
START_BLOCK_FLOOR = 120    # меньше — огрызок; блок не показываем, долю возвращаем


def _weighted_budgets(want: list[int], weight: list[float], total: int,
                      floor: int = START_BLOCK_FLOOR) -> list[int]:
    """Взвешенный water-fill: блок берёт min(запрос, своя доля), неиспользованное
    делится между теми, кому не хватило, пропорционально весам (приоритетам).

    От `_fair_section_budgets` отличается двумя вещами, обеими нужными здесь:
    вес (открытые вопросы важнее runbook'ов) и отсечка огрызков — блок, которому
    досталось меньше `floor`, не показывается вовсе, а его доля возвращается в
    пул. Результат не зависит от порядка блоков.
    """
    n = len(want)
    budgets = [0] * n
    if total <= 0 or not n:
        return budgets
    open_idx = {i for i in range(n) if want[i] > 0 and weight[i] > 0}
    left = total
    while open_idx:
        wsum = sum(weight[i] for i in open_idx)
        if wsum <= 0:
            break
        # доля блока в оставшемся пуле пропорциональна его весу
        fits = {i for i in open_idx if want[i] <= left * weight[i] / wsum}
        if not fits:
            for i in open_idx:
                budgets[i] = int(left * weight[i] / wsum)
            left -= sum(budgets[i] for i in open_idx)
            break
        for i in fits:
            budgets[i] = want[i]
            left -= want[i]
        open_idx -= fits
    # огрызки отбрасываем: обрывок в 30 символов не контекст, а шум
    for i in range(n):
        if 0 < budgets[i] < min(floor, want[i]):
            left += budgets[i]
            budgets[i] = 0
    # освободившееся (и остаток от целочисленного деления) доливаем тем, кто уже
    # показывается и не насытился. Отброшенным не доливаем — иначе огрызок
    # вернулся бы из мёртвых. Порядок задан весом, при равенстве индексом:
    # раздача обязана быть воспроизводимой.
    while left > 0:
        hungry = [i for i in range(n) if budgets[i] and budgets[i] < want[i]]
        if not hungry:
            break
        share = max(1, left // len(hungry))
        for i in sorted(hungry, key=lambda i: (-weight[i], i)):
            if left <= 0:
                break
            take = min(share, want[i] - budgets[i], left)
            budgets[i] += take
            left -= take
    return budgets


















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
    MIN_SCORE = 15  # min hybrid score
    MIN_RERANK = 0.0  # cross-encoder score threshold (BAAI/bge-reranker-base outputs ~[-10, 10])
    parts = []
    blocks: list[_Block] = []

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
                preview = "\n".join(r["preview"].splitlines()[:4])
                scores = f"hybrid: {r.get('score', 0)}"
                if "rerank_score" in r:
                    scores += f", rerank: {r['rerank_score']:.2f}"
                found_items.append(f"### [{r['project']}] {r['title']} ({scores})\n{preview}")
            blocks.append(_Block("found", f"## Найдено ({len(relevant)} релевантных, hybrid+rerank)",
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
                if topic_words & line_words:
                    relevant_lines.append(line)
            blocks.append(_Block("activity", f"## Связанные действия в {target_project}",
                                 relevant_lines[:3], weight=1.5))

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
    dl_items = []
    for d in deadlines:
        when = "истёк" if d["days_left"] < 0 else f"осталось {d['days_left']} дн"
        dl_items.append(f"- **{d['title'][:90]}** — {d['date']}, {when}")
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
        if dep_results:
            dep_results.sort(key=lambda r: -r.get("score", 0))
            dep_items = []
            for r in dep_results[:2]:
                preview = "\n".join(r["preview"].splitlines()[:3])
                dep_items.append(f"### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}")
            blocks.append(_Block("deps", f"## Из зависимых проектов ({', '.join(deps)})",
                                 dep_items, weight=1.0, sep="\n\n"))

    # 5. Relevant decisions (brief, only high-score)
    decision_results = await _whoosh_async(topic, project=target_project, limit=10)
    decisions_found = []
    for r in decision_results:
        if r.get("score", 0) < 30:
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
            decisions_found.append(f"- **{r['title']}** — {decision_line[:100]}")
    blocks.append(_Block("decisions", "## Решения по теме", decisions_found[:3], weight=1.2))

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


def _project_from_cwd(cwd: str) -> Optional[str]:
    """Сопоставить cwd с существующим проектом по имени директории.

    Алгоритм: ищем по компонентам пути (от глубокого к мелкому) первое
    совпадение с проектом из list_projects. Например:
      cwd = /home/user/dev/myapp/backend → проверяем 'backend', потом 'myapp', потом 'dev'
    Возвращает первое найденное имя проекта (lowercase) или None.
    """
    import memory_compiler.config as _cfg
    if not cwd:
        return None
    # Нормализуем разделители (Windows / Unix)
    parts = re.split(r"[/\\]", cwd.strip())
    parts = [p for p in parts if p]  # strip empty
    projects_set = set(p.lower() for p in _cfg.PROJECTS)
    # Iterate from deepest dir towards root — last (most specific) match wins
    for component in reversed(parts):
        normalized = component.lower().strip()
        if normalized in projects_set:
            return normalized
    return None


async def route_project(text: str = "", cwd: str = "", top_k: int = 3) -> list[TextContent]:
    """Авто-определение лучших проектов под текст запроса.

    Параметры:
      text  — описание задачи / упоминание сущности (опционально)
      cwd   — текущий рабочий каталог клиента (опционально, СИЛЬНЫЙ сигнал)
      top_k — сколько кандидатов вернуть

    Алгоритм:
      0. Если cwd содержит имя существующего проекта → возвращаем его с score 100 (override)
      1. Substring match — имя проекта целиком в тексте (вес: 50)
      2. Token overlap — слова из имени проекта в тексте (вес: 30)
      3. Content match — поиск text в статьях проекта (вес: 20)

    Используется клиентом (скил/CLI) когда нет явного project. Без хардкода клиентов.
    """
    import memory_compiler.config as _cfg

    # 0. CWD override — сильнейший сигнал. Если рабочий каталог совпадает с проектом, берём его.
    if cwd:
        cwd_proj = _project_from_cwd(cwd)
        if cwd_proj:
            return [TextContent(type="text", text=(
                f"# Route project\n\n"
                f"*cwd:* `{cwd}` → проект `{cwd_proj}` (score: 100, источник: cwd-match)\n\n"
                f"→ Используй `project=\"{cwd_proj}\"`."
            ))]

    text_lower = (text or "").lower()
    if not text_lower.strip() and not cwd:
        return [TextContent(type="text", text="# Route project\n\n*Пустой запрос и нет cwd — нечего роутить.*")]

    text_tokens = set(re.findall(r"[\wа-яё-]{3,}", text_lower))

    # Получить актуальный список проектов
    projects = [p for p in _cfg.PROJECTS if p not in ("daily",)]
    scores: dict[str, float] = {}

    for proj in projects:
        proj_lower = proj.lower()
        s = 0.0

        # 1. Substring — имя проекта целиком
        if proj_lower in text_lower:
            s += 50

        # 2. Token overlap — части имени проекта (по - и _)
        proj_tokens = set(re.split(r"[-_]", proj_lower))
        proj_tokens.discard("")
        proj_tokens -= {"ut", "buh", "site", "ru", "khv"}  # generic suffixes
        overlap = proj_tokens & text_tokens
        if proj_tokens:
            s += 30 * (len(overlap) / len(proj_tokens))

        # 3. Content match — есть ли в проекте статьи на тему текста
        if text_lower.strip():
            try:
                results = await _whoosh_async(text, project=proj, limit=3)
                content_score = sum(r.get("score", 0) for r in results) / 100
                s += min(20, content_score * 2)
            except Exception:
                pass

        if s > 0:
            scores[proj] = round(s, 1)

    if not scores:
        proj_list = ", ".join(projects[:10]) + ("..." if len(projects) > 10 else "")
        return [TextContent(type="text", text=(
            f"# Route project\n\n"
            f"*Не удалось подобрать проект для: «{text[:100]}»*\n\n"
            f"Доступные проекты: {proj_list}\n\n"
            f"Если уверен — передай `project=` явно. Иначе используй `general`."
        ))]

    # Детерминизм: при равном score тай-брейк по алфавиту, а не по порядку PROJECTS
    # (= os.listdir — зависит от ФС). Иначе один и тот же запрос в разных сессиях
    # роутился в разные проекты → кросс-проектные дубли статей.
    sorted_scores = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    parts = [f"# Route project\n\n*Запрос:* «{text[:120]}»\n"]
    parts.append("\n## Топ кандидатов\n")
    for proj, sc in sorted_scores:
        confidence = "высокая" if sc >= 50 else ("средняя" if sc >= 25 else "низкая")
        parts.append(f"- **{proj}** — score {sc} ({confidence})")

    best, best_score = sorted_scores[0]
    # Почти равные сильные кандидаты: алфавитный тай-брейк детерминирован, но не
    # «правилен» — молчаливый выбор одного из двух и порождал дубли. Просим уточнить.
    ambiguous = (len(sorted_scores) > 1 and sorted_scores[1][1] >= 25
                 and best_score - sorted_scores[1][1] < 5)
    if ambiguous:
        second = sorted_scores[1][0]
        parts.append(f"\n→ Неоднозначно: «{best}» и «{second}» почти равны (разрыв "
                     f"{round(best_score - sorted_scores[1][1], 1)}) — уточни проект явно "
                     f"или используй `general`.")
    elif best_score >= 25:
        parts.append(f"\n→ Используй `project=\"{best}\"` для save/start_task.")
    else:
        parts.append("\n→ Все совпадения слабые — лучше уточнить у пользователя или использовать `general`.")

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


# ─── Управление проектами ────────────────────────────────────────────────────


async def init_schema(project: str) -> list[TextContent]:
    """Create a _schema.md template in the project directory (Karpathy LLM Wiki pattern).

    Idempotent: if _schema.md already exists, returns a hint without overwriting.
    The schema is a human-edited contract — entities, relations, stylistic conventions —
    that lint and save_lesson can later use to enforce consistency.
    """
    proj_dir = safe_project_dir(project)
    schema_path = proj_dir / "_schema.md"
    if schema_path.exists():
        return [TextContent(type="text", text=(
            f"ℹ️ _schema.md уже существует в {project}. "
            f"Открой и отредактируй вручную: {schema_path}"
        ))]

    template = f"""# Schema: {project}

Контракт проекта — какие сущности существуют, какие связи бывают, какой стиль статей.
Используется `lint` и `save_lesson` для проверки соответствия (TODO).

## Сущности

<!-- Перечисли типы статей в проекте и их обязательные поля. Пример:
- ticket — заявка клиента (id, status, client, assignee)
- runbook — пошаговая инструкция (steps, verification)
- decision — архитектурное решение (alternatives, reasoning)
-->

## Связи

<!-- Какие отношения между сущностями. Пример:
- ticket → client (поле client в frontmatter)
- ticket → runbook (через общий тег)
-->

## Stylistic

<!-- Стилистические соглашения проекта. Пример:
- Все runbook-статьи имеют чекбоксы `- [ ]` для шагов
- В tracking_*.md current.version всегда semver
- Заголовки секций на русском
-->

## Glossary

<!-- Специфические термины и аббревиатуры проекта. -->
"""
    schema_path.write_text(template, encoding="utf-8")
    log_event(project, "init_schema", "_schema.md template created")
    return [TextContent(type="text", text=(
        f"✅ Создан шаблон _schema.md в {project}. "
        f"Отредактируй файл: добавь сущности, связи, conventions проекта."
    ))]


async def add_project(name: str) -> list[TextContent]:
    import memory_compiler.config as _cfg
    name = re.sub(r'[^\w\-]', '', name.lower().strip())
    if not name:
        return [TextContent(type="text", text="Некорректное имя проекта.")]
    proj_path = KNOWLEDGE_DIR / name
    if proj_path.exists():
        return [TextContent(type="text", text=f"Проект '{name}' уже существует.")]
    proj_path.mkdir(parents=True, exist_ok=True)
    _cfg.PROJECTS[:] = _discover_projects()
    await asyncio.to_thread(git_commit, f"add project: {name}")
    return [TextContent(type="text", text=f"\u2705 Проект '{name}' создан. Всего проектов: {len(_cfg.PROJECTS)}")]


async def remove_project(name: str, confirm: bool = False) -> list[TextContent]:
    import memory_compiler.config as _cfg
    from memory_compiler.storage import normalize_project
    name = normalize_project(name)
    proj_path = KNOWLEDGE_DIR / name
    if not proj_path.exists():
        return [TextContent(type="text", text=f"Проект '{name}' не найден.")]
    # Посчитать статьи
    articles = list(proj_path.glob("*.md"))
    # Require explicit confirmation to delete project with articles
    if articles and not confirm:
        return [TextContent(type="text", text=f"⚠️ Проект '{name}' содержит {len(articles)} статей. Для удаления передайте confirm=True. Это действие необратимо.")]
    if articles:
        keys = [f"{name}/{md.name}" for md in articles]
        for key in keys:
            article_meta.pop(key, None)  # loop: dict-op (гонка с track_access при выносе)

        def _rm_embeds():  # индекс-путь в worker-потоке (persist=False в цикле, один персист)
            for key in keys:
                _search.remove_embedding(key, persist=False)
            _search.persist_embeddings()
        await asyncio.to_thread(_rm_embeds)
    # Удалить папку (блокирующий I/O — вне event loop)
    await asyncio.to_thread(shutil.rmtree, str(proj_path))
    save_article_meta()
    _cfg.PROJECTS[:] = _discover_projects()
    await asyncio.to_thread(_search.delete_project_documents, name)  # точечно, вне event loop
    await asyncio.to_thread(regenerate_index)
    await asyncio.to_thread(git_commit, f"remove project: {name} ({len(articles)} articles)")
    return [TextContent(type="text", text=f"\U0001f5d1\ufe0f Проект '{name}' удалён ({len(articles)} статей). Осталось проектов: {len(_cfg.PROJECTS)}")]


async def list_projects() -> list[TextContent]:
    import memory_compiler.config as _cfg
    _cfg.PROJECTS[:] = _discover_projects()
    lines = [f"# Проекты ({len(_cfg.PROJECTS)})\n"]
    for proj in _cfg.PROJECTS:
        proj_path = KNOWLEDGE_DIR / proj
        if proj_path.exists():
            articles = [f for f in proj_path.glob("*.md") if not f.name.startswith("_")]
            size = sum(f.stat().st_size for f in articles)
            lines.append(f"- **{proj}** \u2014 {len(articles)} статей, {round(size/1024, 1)} KB")
        else:
            lines.append(f"- **{proj}** \u2014 пуст")
    return [TextContent(type="text", text="\n".join(lines))]










# ─── Project dependencies ─────────────────────────────────────────────────


async def set_project_deps(project: str, depends_on: list) -> list[TextContent]:
    """Set project dependencies."""
    # Validate projects exist
    for dep in depends_on:
        if dep == project:
            return [TextContent(type="text", text=f"Проект не может зависеть от себя.")]

    write_project_deps(project, depends_on)
    await asyncio.to_thread(git_commit, f"deps: {project} -> {', '.join(depends_on)}")
    return [TextContent(type="text", text=f"\U0001f517 Зависимости {project}: {', '.join(depends_on) if depends_on else 'нет'}")]


async def get_project_deps(project: str) -> list[TextContent]:
    """Get project dependencies."""
    deps = read_project_deps(project)
    if not deps:
        return [TextContent(type="text", text=f"Проект {project} не имеет зависимостей.")]
    return [TextContent(type="text", text=f"\U0001f517 {project} зависит от: {', '.join(deps)}")]


























# ─── Knowledge gap detector ───────────────────────────────────────────────

# ─── Отчёты живут в handlers_reports (v1.64.0) ───────────────────────────────
# Реэкспорт, а не переезд по вызывающим: tools.py и 26 имён в тестах ходят
# через handlers.<имя>, и ломать этот адрес ради разреза файла незачем.
from memory_compiler.handlers_reports import (  # noqa: E402,F401
    lint, gap_report, consolidate, knowledge_gap, get_summary, article_history,
    stale_facts, stale_summary, _scan_stale, _link_targets, _base_link_index,
    SECRET_POINTER_RE, _MD_LINK_RE, _WIKI_LINK_RE, _strip_code,
    _STALE_CACHE, _STALE_TTL,
)


# ─── Поиск и ответы живут в handlers_search (v1.83.0) ────────────────────────
# Реэкспорт, а не переезд по вызывающим: tools.py и тесты ходят через
# handlers.<имя>, и ломать этот адрес ради разреза файла незачем.
from memory_compiler.handlers_search import (  # noqa: E402,F401
    search, get_context, ask, ask_sources, ask_fragment,
    search_by_tag, search_snippets, search_error, search_decisions,
    attach_corrections, _render_search_results, _fit_preview, _query_words,
    _resource_links, _rerank_async, _scores, _superseded_note,
    search_payload_var, SEARCH_BUDGET, SEARCH_HEAD, SEARCH_HEAD_WEIGHT,
    SEARCH_CANDIDATE_POOL, SEARCH_RERANK_BUDGET_S, RERANK_ENABLED,
    ASK_TOP_K, ASK_HEAD_LINES, _QUERY_STOP,
)


# ─── Домен статей живёт в handlers_articles (v1.84.0) ────────────────────────
# Реэкспорт, а не переезд по вызывающим: tools.py, тесты и handlers_reports
# (_validate_repo_path отложенно) ходят через handlers.<имя>.
from memory_compiler.handlers_articles import (  # noqa: E402,F401
    save_lesson, edit_article, delete_article, read_article, save_contexts,
    context_gaps, save_runbook, get_runbook, save_decision, save_from_template,
    list_templates, save_secret, save_tracking, get_current, save_compact,
    compile, ingest, import_obsidian, git_capture, backlinks,
    _index_embed, _is_log_heading, _body_sections, _fair_section_budgets,
    _norm_ws, _manual_link_body, _line_links_to, _collect_backlinks,
    _parse_daily_entries, _validate_repo_path,
    _CTX_INSTRUCTIONS, _CTX_FULLTEXT_CAP, _AUTO_LINK_BLOCKS, _ALLOWED_REPO_ROOTS,
    _SINCE_SAFE_RE, _MAX_RAW_INPUT,
)
