"""Поиск и ответы: search, ask, тематические search_* и рендер выдачи.

Вынесено из handlers.py в v1.83.0: файл дорос до 3063 строк. Шов выбран замером
связности — константы поиска (пулы, бюджеты выдачи, стоп-слова), ContextVar
структурированной выдачи и хелперы рендера используются ТОЛЬКО этими функциями
и уезжают вместе с ними.

⚠️ ДВА ХЕЛПЕРА ИМПОРТИРУЮТСЯ ОТЛОЖЕННО, внутри функций: `_whoosh_async` (нужен
ещё route_project в handlers) и `_weighted_budgets` (нужен ещё start_task, и его
патчат девять тестов). Импорт на уровне модуля дал бы цикл handlers ↔ search,
а отложенный вдобавок сохраняет тестам патч на handlers: имя берётся из модуля
в момент вызова, а не связывается при импорте.
"""

import asyncio
import os
import re
from contextvars import ContextVar

from mcp.types import TextContent, ResourceLink

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, track_access, is_secret_article,
)
from memory_compiler.storage import (
    article_title_tags, make_preview, safe_project_dir, superseded_by,
    extract_snippets, extract_errors,
)


# ─── get_context ─────────────────────────────────────────────────────────────


async def get_context(project: str, query: str = None) -> list[TextContent]:
    # Отложенно: см. шапку модуля — рвёт цикл и сохраняет тестам патч на handlers.
    from memory_compiler.handlers import _whoosh_async
    if query:
        # Wider pool for reranker — top results refined by cross-encoder
        results = await _whoosh_async(query, project=project, limit=10)
        cross = await _whoosh_async(query, project="all", limit=10) if project != "all" else []
        seen = {r["file"] for r in results}
        for r in cross:
            if r["file"] not in seen and r["project"] != project:
                results.append(r)
                if len(results) >= 15:
                    break
        if not results:
            return [TextContent(type="text", text=f"Ничего не найдено по '{query}' в {project}.")]
        results = await _rerank_async(query, results, top_k=5)
        out = [f"# Контекст: {project} (query: {query})\n"]
        for r in results:
            preview = "\n".join(r["preview"].splitlines()[:8])
            scores = f"score: {r['score']}"
            if "rerank_score" in r:
                scores += f", rerank: {r['rerank_score']:.2f}"
            out.append(f"---\n### [{r['project']}] {r['title']} ({scores})\n{preview}\n")
        return [TextContent(type="text", text="\n".join(out))]
    else:
        proj_path = safe_project_dir(project)
        articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not articles:
            return [TextContent(type="text", text=f"База знаний по '{project}' пуста.")]
        selected = [(a, a.read_text(encoding="utf-8")) for a in articles[:5]]
        out = [f"# Контекст: {project}\n"]
        for path, text in selected:
            # make_preview, как в ветке с query выше: срез [:8] по сырому файлу
            # отдавал YAML-frontmatter — из 24 строк превью контенту принадлежало 0.
            # Один и тот же инструмент отвечал по-разному с запросом и без него.
            preview = make_preview(text, n=8)
            out.append(f"---\n### {path.stem}\n{preview}\n")
        return [TextContent(type="text", text="\n".join(out))]


# ─── search ──────────────────────────────────────────────────────────────────

# Бюджет времени на cross-encoder rerank. Если модель холодная (лениво грузится
# при первом запросе на NAS) или кандидатов много — predict может не уложиться в
# MCP-таймаут клиента и весь запрос падал в -32001, теряя уже найденные hybrid-хиты.
# По истечении бюджета отдаём результат БЕЗ rerank (мягкая деградация: hybrid-порядок
# хуже reranked, но это лучше пустой ошибки). Настраивается env SEARCH_RERANK_BUDGET_S.
SEARCH_RERANK_BUDGET_S = float(os.environ.get("SEARCH_RERANK_BUDGET_S", "20"))


# Размер пула кандидатов под reranker. Было 20, но bge-reranker-v2-m3 на слабом CPU
# (NAS J4125) реранкает ВСЕ кандидаты одним predict — 20 пар часто не укладывались в
# бюджет SEARCH_RERANK_BUDGET_S и rerank отваливался (мягкая деградация без reranker).
# 10 даёт ~2x меньше forward-pass'ей при небольшой потере recall. Тюнится env.
SEARCH_CANDIDATE_POOL = int(os.environ.get("SEARCH_CANDIDATE_POOL", "10"))


# Cross-encoder reranker ВЫКЛЮЧЕН по умолчанию. Замерено 2026-07-18 харнессом
# scripts/eval_retrieval.py на 132 РЕАЛЬНЫХ запросах аудит-лога (ground truth —
# статьи, которые действительно открыли после поиска):
#   hybrid         MRR 0.4634  recall@1 0.3636  recall@5 0.5833  recall@10 0.6515  [0.45 с/запрос]
#   hybrid+rerank  MRR 0.4535  recall@1 0.3561  recall@5 0.5758  recall@10 0.6515  [14.5 с/запрос]
# Прироста нет (сдвиг ровно по ОДНОМУ запросу на уровень — шум) при цене ×32.
# recall@10 совпал структурно: пул кандидатов = 10 и меряем на @10, значит reranker
# лишь переставляет те же 10 документов и новых внести не может. Польза cross-encoder
# в литературе берётся из переранжирования БОЛЬШОГО пула (50-100) в короткий топ —
# вытащить релевантное с 40-го места; при пуле 10 спасать нечего, а поднять пул на этом
# CPU невозможно (14.5 с за 10 кандидатов → ~70 с за 50). Включить: RERANK_ENABLED=1 —
# осмысленно только вместе с бо́льшим SEARCH_CANDIDATE_POOL и/или лёгкой моделью,
# и обязательно с повторным замером тем же харнессом.
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "false").lower() in ("1", "true", "yes")


async def _rerank_async(query: str, results: list[dict], top_k: int) -> list[dict]:
    """rerank под бюджетом времени в потоке. При таймауте/ошибке — best-effort: отдаём
    hybrid-результаты как есть (обрезанные до top_k) вместо -32001. wait_for отменяет
    ожидание, но фоновый поток допишет predict вхолостую — результат уже у пользователя.

    При выключенном reranker'е выходим СРАЗУ, не запуская поток. Выставить
    SEARCH_RERANK_BUDGET_S=0 было бы недостаточно: wait_for снял бы ожидание, но
    фоновый поток всё равно досчитал бы predict и сжёг те же ~14.5 с CPU впустую."""
    if not RERANK_ENABLED:
        return results[:top_k]
    from memory_compiler.search import rerank
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(rerank, query, results, top_k=top_k),
            timeout=SEARCH_RERANK_BUDGET_S,
        )
    except (asyncio.TimeoutError, Exception):
        return results[:top_k]


def _resource_links(items) -> list[ResourceLink]:
    """Построить ResourceLink на memory://<проект>/<файл> для результатов поиска.
    items: iterable dict'ов с ключами project/file (+ optional title/desc). Секреты
    (secret_) пропускаются — как ресурс недоступны; дубли по project/file схлопываются.
    Клиент (Claude Desktop) рендерит их как кликабельные ссылки в выводе инструмента."""
    links: list[ResourceLink] = []
    seen: set[str] = set()
    for r in items:
        project, filename = r.get("project"), r.get("file")
        if not project or not filename or filename.startswith("secret_"):
            continue
        key = f"{project}/{filename}"
        if key in seen:
            continue
        seen.add(key)
        links.append(ResourceLink(
            type="resource_link",
            uri=f"memory://{project}/{filename}",
            name=key,
            title=r.get("title", "") or "",
            description=r.get("desc", "") or "",
            mimeType="text/markdown",
        ))
    return links


# Структурированная выдача search собирается ЗДЕСЬ, а не из resource-ссылок в
# tools.py. Ссылок на секреты нет НАМЕРЕННО (как ресурс секрет недоступен — это
# верная политика, см. read_resource), но сборка структуры из ссылок молча теряла
# секретные попадания: панель MCP Apps не показывала их вовсе, и счётчик «найдено»
# расходился с текстовой выдачей того же вызова. Ни исключения, ни предупреждения.
# Передаём через ContextVar, а не модульную переменную: он привязан к задаче,
# поэтому параллельные вызовы не перепутают выдачу.
search_payload_var: ContextVar[dict | None] = ContextVar("search_payload", default=None)


async def search(query: str, project: str = "all") -> list[TextContent]:
    from memory_compiler.handlers import _whoosh_async
    # Industry pattern 2026: fetch wider candidate pool, then cross-encoder rerank to final K.
    # Bigger N for reranker → +25-40% precision over hybrid alone (RAG benchmarks).
    results = await _whoosh_async(query, project=project, limit=SEARCH_CANDIDATE_POOL)

    # Авто-фолбэк на project=all: узкий скоуп часто промахивается по общей сущности,
    # физически лежащей в другом проекте (напр. канал уведомлений / общий креденшл).
    # Вместо «Ничего не найдено» переспрашиваем по всем проектам и помечаем выдачу.
    fallback_all = False
    if not results and project != "all":
        results = await _whoosh_async(query, project="all", limit=SEARCH_CANDIDATE_POOL)
        fallback_all = bool(results)

    if not results:
        # Пустую выдачу тоже объявляем явно: иначе панель прочитала бы payload
        # предыдущего вызова и показала чужие результаты под новым запросом.
        search_payload_var.set({"query": query, "count": 0, "results": []})
        return [TextContent(type="text", text=f"Ничего не найдено: '{query}'")]

    results = await _rerank_async(query, results, top_k=8)
    # Поправки к найденному подтягиваются НЕЗАВИСИМО от релевантности и встают
    # выше отменённых статей: в живом случае поправка была в той же выдаче, но
    # ниже, и агент взял верхнюю.
    results = await attach_corrections(results)

    track_access([f"{r['project']}/{r['file']}" for r in results])

    header = f"# Поиск: '{query}'\n"
    if fallback_all:
        header += (f"\n*В проекте «{project}» ничего не найдено — показаны результаты "
                   f"по всем проектам (возможно, общая/кросс-проектная сущность).*\n")
    links: list[ResourceLink] = []
    found: list[dict] = []
    secrets = {}
    for r in results:
        secret = is_secret_article(r.get("preview", ""), r.get("file", ""))
        if secret:
            r["preview"] = f"# {r['title']}\n\n[зашифровано — используй read_article для просмотра]"
        secrets[f"{r['project']}/{r['file']}"] = secret
    # Текст выдачи собирает ОДИН бюджет: голове полное превью, хвосту короткое
    # (см. _render_search_results). Ссылки и структурная выдача строятся по ВСЕМ
    # результатам независимо от того, сколько текста досталось каждому: обрезка
    # превью не должна прятать найденное от панели MCP Apps.
    out = [_render_search_results(results, header, query)]
    for r in results:
        secret = secrets.get(f"{r['project']}/{r['file']}", False)
        scores = _scores(r)
        # Resource link на статью — клиент открывает/прикрепляет как memory://-ресурс.
        # Секреты не линкуем (как ресурс они недоступны).
        if not secret:
            links.append(ResourceLink(
                type="resource_link",
                uri=f"memory://{r['project']}/{r['file']}",
                name=f"{r['project']}/{r['file']}",
                title=r.get("title", ""),
                description=scores,
                mimeType="text/markdown",
            ))
        # А в структурированную выдачу секрет ВХОДИТ — с флагом. Панель покажет его
        # с замком, а открывать будет через read_article (тот расшифровывает), не
        # через memory://. uri у секрета остаётся идентификатором статьи и НЕ
        # разрешается как ресурс — на это и указывает secret.
        found.append({
            "uri": f"memory://{r['project']}/{r['file']}",
            "name": f"{r['project']}/{r['file']}",
            "title": r.get("title", "") or "",
            "score": scores,
            "project": r["project"],
            "file": r["file"],
            "secret": bool(secret),
        })

    search_payload_var.set({"query": query, "count": len(found), "results": found})
    return [TextContent(type="text", text="\n".join(out)), *links]


# ─── ask ─────────────────────────────────────────────────────────────────────


ASK_TOP_K = 5  # ответу нужна горстка точных источников, а не широкая выдача как у search


ASK_HEAD_LINES = 200  # потолок строк шапки; у статей без '### '-секций там лежит всё тело


def ask_fragment(text: str, question: str, limit: int = 300) -> str:
    """Наиболее релевантная вопросу секция статьи, обрезанная до limit символов.

    Секции — по '### ' (запись статьи). Скор секции — сколько РАЗНЫХ значимых слов
    вопроса в ней встретилось. Пустая строка, если не совпало ничего: вызывающий
    подставит preview.

    ⚠️ Шапка статьи в кандидаты НЕ идёт: это метаданные, а не ответ. Раньше шла —
    нулевым куском split'а оказывался YAML-frontmatter + заголовок + **Дата/Проект/
    Теги** + '## Записи'. С появлением contexts:-frontmatter (v1.28.0, ИИ-пересказ
    КАЖДОЙ секции) эта шапка стала матчить почти любой релевантный вопрос и
    выигрывать скор — на живой базе 5 фрагментов из 5 не содержали ни строчки
    контента: сырой YAML либо меташапка. Фильтруем ПОСТРОЧНО (article_body_lines),
    а не выбрасываем нулевой кусок целиком: у ~15% статей (ingest/импорт) нет ни
    одной '### '-секции, и всё тело лежит именно там."""
    q_words = {w.lower() for w in question.split() if len(w) > 2}
    if not q_words:
        return ""
    from memory_compiler.search import _strip_frontmatter
    from memory_compiler.storage import article_body_lines
    sections = _strip_frontmatter(text).split("\n### ")
    sections[0] = "\n".join(article_body_lines(sections[0], limit=ASK_HEAD_LINES))
    best_score, best_sec = 0, ""
    for sec in sections:
        low = sec.lower()
        score = sum(1 for w in q_words if w in low)
        if score > best_score:
            best_score, best_sec = score, sec.strip()
    return best_sec[:limit].strip() if best_score else ""


async def ask_sources(question: str, project: str = "all") -> tuple:
    """Источники для ответа на вопрос: (список источников, был ли фолбэк на все проекты).

    Общее ядро MCP-тула ask (рендерит в текст) и /api/ask (отдаёт JSON) — чтобы
    ассистент и веб-UI отвечали на один вопрос одинаково, а не разными конвейерами.
    Конвейер тот же, что у search: широкий пул кандидатов -> фолбэк на project=all
    -> cross-encoder rerank. Раньше ask брал whoosh top-5 без реранка и без фолбэка,
    т.е. отвечал ХУЖЕ, чем search (реранкер даёт +25-40% precision).
    """
    from memory_compiler.handlers import _whoosh_async
    results = await _whoosh_async(question, project=project, limit=SEARCH_CANDIDATE_POOL)
    fallback_all = False
    if not results and project != "all":
        results = await _whoosh_async(question, project="all", limit=SEARCH_CANDIDATE_POOL)
        fallback_all = bool(results)
    if not results:
        return [], False

    results = await _rerank_async(question, results, top_k=ASK_TOP_K)
    track_access([r["project"] + "/" + r["file"] for r in results])

    sources = []
    for r in results:
        # Секретные статьи не цитируем. ⚠️ Проверять по ФАЙЛУ, а не по preview:
        # _index_safe_text САМ вырезает строку '**Секрет:** да', собирая плейсхолдер
        # из титула и тегов, — в preview признак не выживает, и от проверки оставался
        # только префикс имени 'secret_'. Пока у всех секретов базы префикс есть, это
        # не стреляло, но это ровно тот баг, который чинили в v1.25.0, подключённый
        # к неверному источнику истины: статья с флагом, но без префикса, утекла бы.
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        raw = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        secret = bool(is_secret_article(raw or r.get("preview", ""), r.get("file", "")))
        fragment = ""
        if not secret:
            if raw:
                fragment = ask_fragment(raw, question)
            if not fragment:
                fragment = "\n".join(r["preview"].splitlines()[:5])
        sources.append({
            "project": r["project"],
            "file": r["file"],
            "title": r.get("title", r["file"]),
            "score": r["score"],
            "rerank": round(r["rerank_score"], 3) if "rerank_score" in r else None,
            "fragment": fragment,
            "secret": secret,
        })
    return sources, fallback_all


async def ask(question: str, project: str = "all") -> list[TextContent]:
    sources, fallback_all = await ask_sources(question, project=project)
    if not sources:
        return [TextContent(type="text", text=f"Не найдено информации по: '{question}'")]

    header = f"# Ответ на: {question}\n"
    if fallback_all:
        header += (f"\n*В проекте «{project}» ничего не найдено — показаны результаты "
                   f"по всем проектам (возможно, общая/кросс-проектная сущность).*\n")
    out = [header]
    for s in sources:
        label = "[" + s["project"] + "/" + s["file"] + "]"
        scores = "score: " + str(s["score"])
        if s["rerank"] is not None:
            scores += ", rerank: %.2f" % s["rerank"]
        body = "[зашифровано — используй read_article для просмотра]" if s["secret"] else s["fragment"]
        out.append("---\n**" + label + "** (" + scores + ")\n> " + body + "\n")

    return [TextContent(type="text", text="\n".join(out))]


async def search_by_tag(tag: str, project: str = "all") -> list[TextContent]:
    from memory_compiler.search import _strip_frontmatter

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
            lines = _strip_frontmatter(text).splitlines()
            title = lines[0].lstrip("# ").strip() if lines else md.stem
            for line in lines[:10]:
                if line.lower().startswith("**теги:**"):
                    tags_str = line.split(":", 1)[1].strip()
                    article_tags = [t.strip().lower().strip("*").strip() for t in tags_str.split(",")]
                    if tag_lower in article_tags:
                        preview = make_preview(text)
                        results.append({"title": title, "project": proj, "file": md.name, "preview": preview})
                    break
    if not results:
        return [TextContent(type="text", text=f"Статей с тегом '{tag}' не найдено.")]
    track_access([f"{r['project']}/{r['file']}" for r in results])
    out = [f"# Тег: {tag} ({len(results)} статей)\n"]
    for r in results:
        out.append(f"---\n### [{r['project']}] {r['title']}\n{r['file']}\n")
    return [TextContent(type="text", text="\n".join(out)), *_resource_links(results)]


# ── Бюджет выдачи search (v1.67.0) ──────────────────────────────────────────
# `search` отдавал 8 результатов с превью в 10 строк КАЖДЫЙ — одинаково первому
# и восьмому. Замер 26.08.2026: это 64% всех символов, которые инструменты
# возвращают за неделю (2626 тыс. из 4125 тыс.), медиана выдачи 13132 символа.
# Хвост столько не стоит, и это показали два независимых замера:
#   • baseline retrieval_eval: recall@3 0.667, recall@5 0.78, recall@10 0.84 —
#     позиции 6-8 добавляют около 6% попаданий на ~37% объёма;
#   • 345 пар «запрос → открытая статья»: слова запроса стоят в ЗАГОЛОВКЕ у 76%,
#     в первых трёх строках у 87%, в первых четырёх у 91%; строки 5-10 дают 9%.
# Поэтому голове — полное превью, хвосту — короткое, на всё — общий потолок.
# ⚠️ ПОРЯДОК И СОСТАВ НЕ ТРОГАЕМ: правка про рендер, ранжирование то же.
SEARCH_BUDGET = 7000       # потолок на всю выдачу, символов


SEARCH_HEAD = 3            # позиций с полным превью (по recall@3)


SEARCH_HEAD_WEIGHT = 3.0   # во столько раз голова важнее хвоста при дележе


def _query_words(text: str) -> set[str]:
    """Значимые слова запроса: короткие и служебные выкидываем."""
    return {w for w in re.sub(r"[^а-яёa-z0-9]+", " ", (text or "").lower()).split()
            if len(w) > 3 and w not in _QUERY_STOP}


_QUERY_STOP = {"как", "что", "где", "для", "при", "это", "или", "был", "все",
               "еще", "ещё", "уже", "про", "него", "нужно", "надо"}


def _fit_preview(preview: str, budget: int, qwords: set[str]) -> str:
    """Уместить превью в бюджет, оставляя строки СО СЛОВАМИ ЗАПРОСА.

    ⚠️ ОТБОР ПО ЗАПРОСУ, А НЕ ПЕРВЫЕ N СТРОК — так решил замер. Сжатие хвоста
    первыми строками теряло сигнал: слова запроса оставались в блоке целевой
    статьи у 74% пар против 81% при полном превью (−7 п.п.). Отбор по запросу
    в ТОМ ЖЕ бюджете даёт 79% на хвосте и 82% в голове, то есть возвращает
    почти всё даром. Замер: 418 golden-пар «запрос → открытая статья».

    ⚠️ ПОРЯДОК СТРОК СОХРАНЯЕТСЯ: превью читают как связный текст, а
    перетасованные цитаты читаются как обрывки.
    """
    lines = preview.splitlines()
    if not lines:
        return ""
    head, body = lines[0], lines[1:]
    left = budget - len(head)
    if left <= 0 or not body:
        return head
    hit = [i for i, l in enumerate(body) if qwords & _query_words(l)] if qwords else []
    rest = [i for i in range(len(body)) if i not in set(hit)]
    chosen: set[int] = set()
    for i in hit + rest:                      # сначала совпавшие, потом добор с начала
        need = len(body[i]) + 1
        if need > left:
            continue
        chosen.add(i)
        left -= need
    if not chosen:
        return head
    out, skipped = [head], False
    for i in range(len(body)):
        if i in chosen:
            if skipped:
                out.append("…")
                skipped = False
            out.append(body[i])
        elif out:
            skipped = True
    return "\n".join(out)


async def attach_corrections(results: list[dict]) -> list[dict]:
    """Подтянуть в выдачу поправки к найденным статьям и поставить их ВЫШЕ.

    ⚠️ НЕЗАВИСИМО ОТ РЕЛЕВАНТНОСТИ — в этом весь смысл. В живом случае поправка
    лежала в той же выдаче, но ниже отменённой статьи, и агент взял верхнюю.
    Поправка не обязана быть релевантнее: она обязана быть ВИДНА.
    """
    if not results:
        return results
    have = {(r.get("project"), r.get("file")) for r in results}
    corrections: list[dict] = []
    for r in results:
        link = await asyncio.to_thread(superseded_by, r.get("project", ""), r.get("file", ""))
        if not link:
            continue
        fname, title = link
        r["superseded_by"] = link
        key = (r.get("project"), fname)
        if key in have:
            continue                       # поправка уже в выдаче — только поднимем
        path = KNOWLEDGE_DIR / r["project"] / fname
        if not path.exists():
            continue
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        corrections.append({
            "project": r["project"], "file": fname,
            "title": title or article_title_tags(text, fname)[0],
            "score": r.get("score", 0), "preview": make_preview(text, n=10),
            "is_correction": True,
        })
        have.add(key)
    if not corrections:
        # поправка уже была в выдаче — поднимаем её над отменённой
        return sorted(results, key=lambda r: bool(r.get("superseded_by")))
    return corrections + sorted(results, key=lambda r: bool(r.get("superseded_by")))


def _render_search_results(results: list[dict], header: str = "", query: str = "") -> str:
    """Собрать выдачу поиска в пределах SEARCH_BUDGET.

    Бюджет делится тем же water-fill'ом, что и стартовый контекст: короткий
    результат берёт своё целиком, неиспользованное достаётся длинным, вес задаёт
    позиция. Заголовок остаётся у КАЖДОГО результата — в нём 76% сигнала, и
    безымянная строка в выдаче бесполезна.
    """
    qwords = _query_words(query)
    if not results:
        return header
    want, weight = [], []
    for i, r in enumerate(results):
        preview = r.get("preview", "") or ""
        head_line = f"---\n### [{r['project']}] {r['title']} ({_scores(r)})\n"
        want.append(len(head_line) + len(preview) + 1)
        weight.append(SEARCH_HEAD_WEIGHT if i < SEARCH_HEAD else 1.0)
    # Отложенно: тот же water-fill делит стартовый контекст, и его патчат девять тестов
    # через handlers.<имя> — имя обязано браться из модуля в момент вызова.
    from memory_compiler.handlers import _weighted_budgets
    budgets = _weighted_budgets(want, weight, max(SEARCH_BUDGET - len(header), 0), floor=0)
    parts = [header] if header else []
    for r, bud in zip(results, budgets):
        title_line = f"---\n### [{r['project']}] {r['title']} ({_scores(r)})\n"
        # Отменённая статья не выдаётся молча: предупреждение идёт ПЕРЕД телом —
        # иначе его прочтут уже после того, как поверят содержанию.
        sup = r.get("superseded_by") or _superseded_note(r)
        if sup:
            title_line += (f"⚠️ **Статья отменена** поправкой «{sup[1] or sup[0]}» "
                           f"({sup[0]}) — читать её.\n")
        elif r.get("is_correction"):
            title_line += "✅ **Это поправка** — она отменяет прежний вывод по теме.\n"
        left = bud - len(title_line)
        # ⚠️ Второго среза по строкам здесь НЕТ: превью уже собрано
        # make_preview(n=10) в search.py. Резать одно и то же дважды значит
        # считать бюджет по объёму, которого в выдаче не будет, — тогда он
        # распределяется впустую, и голова получает столько же, сколько хвост.
        body = r.get("preview", "") or ""
        if left <= 0:
            parts.append(title_line)       # заголовок отдаём всегда
            continue
        if len(body) > left:
            body = _fit_preview(body, left, qwords)
        parts.append(title_line + body + "\n")
    return "".join(parts)


def _superseded_note(r: dict):
    """Пометка об отмене из шапки статьи (дешёвое чтение, без обхода базы)."""
    try:
        return superseded_by(r.get("project", ""), r.get("file", ""))
    except Exception:
        return None


def _scores(r: dict) -> str:
    s = f"score: {r['score']}"
    if "rerank_score" in r:
        s += f", rerank: {r['rerank_score']:.2f}"
    return s


# ─── Snippet search ────────────────────────────────────────────────────────


async def search_snippets(query: str, lang: str = None, project: str = "all") -> list[TextContent]:
    """Search code snippets in knowledge base."""
    from memory_compiler.handlers import _whoosh_async
    results = await _whoosh_async(query, project=project, limit=10)
    if not results:
        return [TextContent(type="text", text=f"Сниппетов не найдено: '{query}'")]

    found = []
    for r in results:
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        snippets = extract_snippets(text)
        for s in snippets:
            if lang and s["lang"] != lang:
                continue
            # Check if query words appear in code
            q_words = set(w.lower() for w in query.split() if len(w) > 2)
            code_lower = s["code"].lower()
            matches = sum(1 for w in q_words if w in code_lower)
            if matches > 0:
                found.append({
                    "article": f"{r['project']}/{r['file']}",
                    "lang": s["lang"],
                    "context": s["context"],
                    "code": s["code"][:500],
                    "relevance": matches,
                })

    found.sort(key=lambda x: x["relevance"], reverse=True)
    if not found:
        return [TextContent(type="text", text=f"Сниппетов с '{query}' не найдено.")]

    out = [f"# Сниппеты: '{query}' ({len(found)} найдено)\n"]
    link_items = []
    for s in found[:10]:
        out.append(f"---\n**[{s['article']}]** ({s['lang']}) — {s['context']}\n```{s['lang']}\n{s['code']}\n```\n")
        if "/" in s["article"]:
            p, f = s["article"].split("/", 1)
            link_items.append({"project": p, "file": f, "title": s.get("context", "")})
    return [TextContent(type="text", text="\n".join(out)), *_resource_links(link_items)]


# ─── Error search ──────────────────────────────────────────────────────────


async def search_error(error_text: str, project: str = "all") -> list[TextContent]:
    """Search for similar errors in knowledge base."""
    # Extract key parts from error text
    error_patterns = extract_errors(error_text)

    # Build search query from error patterns + original text
    search_terms = []
    for ep in error_patterns:
        search_terms.append(ep["text"][:50])
    if not search_terms:
        # Fallback: use last line of error (usually the exception)
        lines = error_text.strip().splitlines()
        search_terms = [lines[-1][:100]] if lines else [error_text[:100]]

    query = " ".join(search_terms)[:200]
    from memory_compiler.handlers import _whoosh_async
    results = await _whoosh_async(query, project=project, limit=10)

    # Re-rank by error pattern overlap
    ranked = []
    for r in results:
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        article_errors = extract_errors(text)

        # Score boost for matching error types
        boost = 0
        for ae in article_errors:
            for ep in error_patterns:
                if ae["type"] == ep["type"]:
                    boost += 10
                    # Extra boost for matching error text
                    if ep["text"][:30].lower() in ae["text"].lower():
                        boost += 20
        r["score"] = r.get("score", 0) + boost
        ranked.append(r)

    ranked.sort(key=lambda x: x["score"], reverse=True)
    if not ranked:
        return [TextContent(type="text", text=f"Похожих ошибок не найдено.")]

    track_access([f"{r['project']}/{r['file']}" for r in ranked[:5]])

    out = [f"# Похожие ошибки ({len(ranked)} найдено)\n"]
    for r in ranked[:5]:
        preview = "\n".join(r["preview"].splitlines()[:8])
        out.append(f"---\n### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")
    return [TextContent(type="text", text="\n".join(out)), *_resource_links(ranked[:5])]


async def search_decisions(query: str, project: str = "all") -> list[TextContent]:
    """Search only decision articles."""
    from memory_compiler.handlers import _whoosh_async
    results = await _whoosh_async(query, project=project, limit=15)

    # Filter to decision articles only
    decisions = []
    for r in results:
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            continue
        text = fpath.read_text(encoding="utf-8")
        if "**Тип:** decision" in text or r["file"].startswith("decision_"):
            decisions.append(r)

    if not decisions:
        return [TextContent(type="text", text=f"Решений по '{query}' не найдено.")]

    track_access([f"{r['project']}/{r['file']}" for r in decisions])
    out = [f"# Решения: '{query}' ({len(decisions)})\n"]
    for r in decisions:
        preview = "\n".join(r["preview"].splitlines()[:8])
        out.append(f"---\n### [{r['project']}] {r['title']} (score: {r['score']})\n{preview}\n")
    return [TextContent(type="text", text="\n".join(out)), *_resource_links(decisions)]
