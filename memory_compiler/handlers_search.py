"""Поиск и ответы: search, ask, тематические search_* и сборка JSON-выдачи search.

Вынесено из handlers.py в v1.83.0: файл дорос до 3063 строк. Шов выбран замером
связности — константы поиска (пулы), ContextVar
структурированной выдачи используются ТОЛЬКО этими функциями
и уезжают вместе с ними.

⚠️ ХЕЛПЕР ИМПОРТИРУЕТСЯ ОТЛОЖЕННО, внутри функций: `_whoosh_async` (нужен ещё
route_project в handlers). Импорт на уровне модуля дал бы цикл handlers ↔
search, а отложенный вдобавок сохраняет тестам патч на handlers: имя берётся
из модуля в момент вызова, а не связывается при импорте.
"""

import asyncio
import json
import os
import re
from contextvars import ContextVar
from datetime import datetime

from mcp.types import TextContent

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, track_access, is_secret_article,
)
from memory_compiler.storage import (
    article_title_tags, make_preview, safe_project_path, superseded_by,
    extract_snippets, extract_errors, _parse_frontmatter, parse_meta_value,
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
        proj_path = safe_project_path(project)
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


# Структурированная выдача search собирается ЗДЕСЬ, а не из resource-ссылок в
# tools.py. Ссылок на секреты нет НАМЕРЕННО (как ресурс секрет недоступен — это
# верная политика, см. read_resource), но сборка структуры из ссылок молча теряла
# секретные попадания: панель MCP Apps не показывала их вовсе, и счётчик «найдено»
# расходился с текстовой выдачей того же вызова. Ни исключения, ни предупреждения.
# Передаём через ContextVar, а не модульную переменную: он привязан к задаче,
# поэтому параллельные вызовы не перепутают выдачу.
search_payload_var: ContextVar[dict | None] = ContextVar("search_payload", default=None)


def search_json(payload: dict) -> str:
    """Единственная сериализация выдачи search: этот же JSON уходит и в
    structuredContent, и в текстовый блок. ensure_ascii=False — кириллица
    литералом: в escape-форме буква занимает шесть символов."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _search_item(r: dict, secret: bool) -> dict:
    """Результат выдачи — ровно то, что модель видела раньше, без дублей и превью.

    ⚠️ Схема выдачи НЕ меняется (v1.91.0): `score` остаётся строкой, `secret` —
    необязательным полем. Клиенты держат схему в кэше до перезапуска, и число вместо
    строки уронило бы каждый поиск (живой случай 16.09.2026, «must have required
    property 'uri'»). Префикс «score: » и `secret: false` сняты: они повторялись в
    каждом результате и занимали 9–10% выдачи (замер @link28rus 24.09.2026).
    """
    project, file = r["project"], r["file"]
    score = str(r["score"])
    if "rerank_score" in r:
        score += f", rerank: {r['rerank_score']:.2f}"
    item = {
        "title": r.get("title", "") or "",
        "project": project,
        "file": file,
        "score": score,
    }
    # Пометки только когда есть что сказать: пустые поля — это символы в контексте.
    if secret:
        item["secret"] = True
    sup = r.get("superseded_by")
    if sup:
        item["superseded_by"] = sup[0]
    if r.get("is_correction"):
        item["correction"] = True
    return item


def _search_payload(query: str, results: list[dict], secrets: dict,
                    fallback_from: str | None = None) -> dict:
    """Выдача search в порядке ранжирования (поправки уже подняты attach_corrections)."""
    payload: dict = {"query": query, "count": len(results)}
    if fallback_from:
        payload["fallback_from"] = fallback_from
    items = [
        _search_item(r, secrets.get(f"{r['project']}/{r['file']}", False)) for r in results]
    # Поправка могла попасть в выдачу через РАНЖИРОВАНИЕ, а не через
    # attach_corrections, — тогда is_correction у неё нет. Помечаем по факту:
    # её файл назван как superseded_by другого элемента ТОГО ЖЕ проекта.
    targets = {(it["project"], it["superseded_by"]) for it in items if it.get("superseded_by")}
    for it in items:
        if (it["project"], it["file"]) in targets:
            it["correction"] = True
    payload["results"] = items
    return payload


async def search(query: str, project: str = "all") -> list[TextContent]:
    from memory_compiler.handlers import _whoosh_async
    # Industry pattern 2026: fetch wider candidate pool, then cross-encoder rerank to final K.
    results = await _whoosh_async(query, project=project, limit=SEARCH_CANDIDATE_POOL)

    # Авто-фолбэк на project=all: узкий скоуп часто промахивается по общей сущности,
    # физически лежащей в другом проекте (напр. канал уведомлений / общий креденшл).
    fallback_all = False
    if not results and project != "all":
        results = await _whoosh_async(query, project="all", limit=SEARCH_CANDIDATE_POOL)
        fallback_all = bool(results)

    if not results:
        # Пустую выдачу тоже объявляем явно: иначе панель прочитала бы payload
        # предыдущего вызова и показала чужие результаты под новым запросом.
        payload = _search_payload(query, [], {})
        search_payload_var.set(payload)
        return [TextContent(type="text", text=search_json(payload))]

    results = await _rerank_async(query, results, top_k=8)
    # Поправки к найденному подтягиваются НЕЗАВИСИМО от релевантности и встают
    # выше отменённых статей: в живом случае поправка была в той же выдаче, но
    # ниже, и агент взял верхнюю.
    results = await attach_corrections(results)
    results = await _mark_superseded_corrections(results)
    track_access([f"{r['project']}/{r['file']}" for r in results])

    # Секрет входит в выдачу с флагом (панель рисует замок, открывает через
    # read_article), но без содержимого — превью в выдаче нет вовсе.
    secrets = {f"{r['project']}/{r['file']}": is_secret_article(r.get("preview", ""), r.get("file", ""))
               for r in results}
    # Одна форма выдачи (v1.87.0): тот же JSON уйдёт и в structuredContent (tools.py).
    # Markdown-рендер и resource_link модель в Claude Code не видела ни разу.
    payload = _search_payload(query, results, secrets, project if fallback_all else None)
    search_payload_var.set(payload)
    return [TextContent(type="text", text=search_json(payload))]


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


# ── search_by_tag (v1.91.0) ─────────────────────────────────────────────────
# Раньше: обход всей базы прямо на loop, превью на каждое попадание (строилось и
# выбрасывалось), все статьи без ограничения в порядке обхода каталога и
# resource_link на каждую строку. Тег bugfix — 985 статей, 150 тыс. символов;
# клиент резал ответ, и модель видела случайное начало списка (замер 24.09.2026).
TAG_LIMIT_DEFAULT = 30
TAG_LIMIT_MAX = 200

_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DATE_DMY_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})")


def _normalize_date(raw: str) -> str | None:
    """ГГГГ-ММ-ДД или None. «24.09.2025» лексикографически обгоняет ISO-даты
    (сравнение строкой видит только цифры, не календарь) — поэтому ДД.ММ.ГГГГ
    переводится в ISO, а всё, что не подходит ни под один формат, не считается
    датой вовсе (следующий источник, в итоге mtime файла)."""
    raw = raw.strip()
    if _DATE_ISO_RE.match(raw):
        return raw[:10]
    m = _DATE_DMY_RE.match(raw)
    if m:
        d, mth, y = m.groups()
        return f"{y}-{mth}-{d}"
    return None


def _article_date(text: str, path) -> str:
    """ГГГГ-ММ-ДД: «Обновлено», иначе «Дата» из шапки тела, иначе дата файла.

    Шапка кончается на первом заголовке любого уровня: «Дата:» внутри записи
    (daily-агрегаты) датой статьи не считается. Значение принимается только в
    распознанном формате (ISO или ДД.ММ.ГГГГ) — иначе оно не дата, а следующий
    источник (mtime)."""
    found: dict[str, str] = {}
    seen: set[str] = set()
    for line in _parse_frontmatter(text)[1].splitlines()[1:]:
        if line.startswith("#"):
            break
        low = line.lower()
        for label in ("**обновлено:**", "**дата:**"):
            if low.startswith(label) and label not in seen:
                seen.add(label)
                normalized = _normalize_date(parse_meta_value(line))
                if normalized:
                    found[label] = normalized
    date = found.get("**обновлено:**") or found.get("**дата:**")
    return date or datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def _scan_tag(tag: str, projects: list[str]) -> list[dict]:
    """Все статьи с тегом. Синхронно: обход всей базы — звать через to_thread
    (имя внесено в HEAVY tests/test_no_blocking_calls.py)."""
    tag = tag.lower().strip()
    hits = []
    for proj in projects:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            text = md.read_text(encoding="utf-8")
            title, tags_line = article_title_tags(text, md.stem)
            tags = {t.strip().strip("*").strip().lower() for t in tags_line.split(",")}
            if tag in tags:
                hits.append({"title": title, "project": proj, "file": md.name,
                             "date": _article_date(text, md)})
    return hits


async def search_by_tag(tag: str, project: str = "all",
                        limit: int = TAG_LIMIT_DEFAULT) -> list[TextContent]:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = TAG_LIMIT_DEFAULT
    limit = max(1, min(limit, TAG_LIMIT_MAX))
    check_projects = PROJECTS if project == "all" else [project]
    hits = await asyncio.to_thread(_scan_tag, tag, check_projects)
    if not hits:
        return [TextContent(type="text", text=f"Статей с тегом '{tag}' не найдено.")]
    # Два стабильных прохода: дата по убыванию, при равенстве — проект и файл.
    hits.sort(key=lambda h: (h["project"], h["file"]))
    hits.sort(key=lambda h: h["date"], reverse=True)
    shown = hits[:limit]
    track_access([f"{h['project']}/{h['file']}" for h in shown])
    head = f"# Тег: {tag} — {len(shown)} из {len(hits)}, свежие сверху"
    # Счёт по проектам — полный, и по тем, что не попали в показ: по нему видно,
    # куда сузить `project`.
    per_project: dict[str, int] = {}
    for h in hits:
        per_project[h["project"]] = per_project.get(h["project"], 0) + 1
    if len(per_project) > 1:
        ranked = sorted(per_project.items(), key=lambda pc: (-pc[1], pc[0]))
        head += " (по проектам: " + ", ".join(f"{p} {c}" for p, c in ranked) + ")"
    out = [head]
    out += [f"- [{h['project']}] {h['title']} — {h['file']} ({h['date']})" for h in shown]
    hidden = len(hits) - len(shown)
    if hidden:
        out.append(f"*…ещё {hidden} — сузь `project`, подними `limit` или ищи через `search`*")
    return [TextContent(type="text", text="\n".join(out))]


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


async def _mark_superseded_corrections(results: list[dict]) -> list[dict]:
    """Поправка, которую attach_corrections сама подтянула в выдачу, может быть
    отменена следующей поправкой (цепочка А→Б→В).

    attach_corrections проверяет superseded_by только у ИСХОДНЫХ найденных статей,
    а не у поправок, которые подтянула сама, — такая поправка уезжала в выдачу с
    correction: true, но без superseded_by, и цепочка обрывалась молча.
    """
    for r in results:
        if r.get("is_correction") and not r.get("superseded_by"):
            link = await asyncio.to_thread(superseded_by, r["project"], r["file"])
            if link:
                r["superseded_by"] = link
    return results


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
    for s in found[:10]:
        out.append(f"---\n**[{s['article']}]** ({s['lang']}) — {s['context']}\n```{s['lang']}\n{s['code']}\n```\n")
    return [TextContent(type="text", text="\n".join(out))]


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
    return [TextContent(type="text", text="\n".join(out))]


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
    return [TextContent(type="text", text="\n".join(out))]
