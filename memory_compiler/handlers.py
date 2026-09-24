"""
Tool handler implementations for memory-compiler MCP server.
All async functions return list[TextContent].
"""
import asyncio
import re
import shutil
from typing import Optional

from mcp.types import TextContent

from memory_compiler.config import (
    KNOWLEDGE_DIR, article_meta, save_article_meta, _discover_projects,
)
from memory_compiler.search import whoosh_search
# Модульный импорт: _embeddings/_embed_texts переприсваиваются в rebuild_embeddings
# (свап нового dict). Импорт `from search import _embeddings` заморозил бы ССЫЛКУ на
# старый объект — delete/remove чистили бы устаревший dict, а semantic-поиск ходил по
# новому → удалённая статья оставалась бы фантомом. Обращаемся через модуль.
import memory_compiler.search as _search
from memory_compiler.storage import (
    regenerate_index, git_commit,
    read_project_deps, write_project_deps,
    log_event, safe_project_dir, project_article_count, project_key,
)











async def _whoosh_async(query: str, project: str = "all", limit: int = 10) -> list[dict]:
    """whoosh_search в потоке: он CPU-тяжёлый (semantic dot-product по всем эмбеддингам +
    при холодном старте ленивая загрузка embed-модели). На event loop он замораживал весь
    сервер (/api/health, параллельные запросы). Общий помощник для всех async-хендлеров."""
    return await asyncio.to_thread(whoosh_search, query, project=project, limit=limit)















# ─── lint ────────────────────────────────────────────────────────────────────




















# ─── get_summary ─────────────────────────────────────────────────────────────
























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
























def _cwd_candidates(cwd: str) -> list[tuple[str, int]]:
    """Проекты, совпавшие с cwd, в порядке предпочтения: [(имя, статей), ...].

    Компоненты пути перебираются от глубокого к мелкому; берётся самый глубокий
    уровень, где совпало хоть что-то:
      cwd = /home/user/dev/myapp/backend → 'backend', потом 'myapp', потом 'dev'
    Каталог сравнивается с проектом по storage.project_key — без регистра, дефисов
    и подчёркиваний: клон D:\\MCP\\MemoryCompiler и проект memory-compiler одно и то же.
    На одном уровне совпасть могут несколько проектов (memory-compiler и
    memorycompiler). Первым идёт тот, где больше статей, пустой — последним: до
    v1.90.3 чтение заводило каталог под любое угаданное имя, такие двойники
    доживают в базах, и выбор их по cwd уводил start_task/get_active_context в
    пустоту. Статьи считает storage.project_article_count — без mkdir, роутинг
    каталогов не заводит. При равенстве — буквальное совпадение имени, затем
    алфавит (выбор воспроизводим).
    """
    import memory_compiler.config as _cfg
    if not cwd:
        return []
    # Нормализуем разделители (Windows / Unix)
    parts = [p for p in re.split(r"[/\\]", cwd.strip()) if p]
    by_key: dict[str, list[str]] = {}
    for proj in sorted(set(p.lower() for p in _cfg.PROJECTS)):
        key = project_key(proj)
        if key:
            by_key.setdefault(key, []).append(proj)
    for component in reversed(parts):
        literal = component.lower().strip()
        matched = by_key.get(project_key(component))
        if not matched:
            continue
        counted = [(p, project_article_count(p)) for p in matched]
        counted.sort(key=lambda pc: (-pc[1], pc[0] != literal, pc[0]))
        return counted
    return []


def _project_from_cwd(cwd: str) -> Optional[str]:
    """Сопоставить cwd с существующим проектом по имени директории.

    Возвращает лучший проект из _cwd_candidates (lowercase) или None.
    """
    candidates = _cwd_candidates(cwd)
    return candidates[0][0] if candidates else None


async def route_project(text: str = "", cwd: str = "", top_k: int = 3) -> list[TextContent]:
    """Авто-определение лучших проектов под текст запроса.

    Параметры:
      text  — описание задачи / упоминание сущности (опционально)
      cwd   — текущий рабочий каталог клиента (опционально, СИЛЬНЫЙ сигнал)
      top_k — сколько кандидатов вернуть

    Алгоритм:
      0. Если cwd содержит имя существующего проекта (без учёта регистра, '-' и '_')
         → возвращаем его с score 100 (override); из нескольких — со статьями
      1. Substring match — имя проекта целиком в тексте (вес: 50)
      2. Token overlap — слова из имени проекта в тексте (вес: 30)
      3. Content match — поиск text в статьях проекта (вес: 20)

    Используется клиентом (скил/CLI) когда нет явного project. Без хардкода клиентов.
    """
    import memory_compiler.config as _cfg

    # 0. CWD override — сильнейший сигнал. Если рабочий каталог совпадает с проектом, берём его.
    if cwd:
        candidates = _cwd_candidates(cwd)
        if candidates:
            cwd_proj = candidates[0][0]
            # Остальные совпавшие называем вслух: пустой двойник в списке проектов
            # иначе так и висит незамеченным, а агент продолжает его угадывать.
            others = ", ".join(f"`{p}` ({n} статей)" for p, n in candidates[1:])
            also = f"Каталогу соответствуют и: {others}.\n\n" if others else ""
            return [TextContent(type="text", text=(
                f"# Route project\n\n"
                f"*cwd:* `{cwd}` → проект `{cwd_proj}` (score: 100, источник: cwd-match)\n\n"
                f"{also}"
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
    attach_corrections, _mark_superseded_corrections, search_json,
    _search_payload, _search_item,
    _rerank_async,
    search_payload_var,
    SEARCH_CANDIDATE_POOL, SEARCH_RERANK_BUDGET_S, RERANK_ENABLED,
    ASK_TOP_K, ASK_HEAD_LINES,
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


# ─── Домен сессий живёт в handlers_sessions (v1.86.0) ────────────────────────
# Реэкспорт, а не переезд по вызывающим: tools.py и тесты ходят через
# handlers.<имя>. В ядре остаются хелперы, которые домен тянет отложенно:
# _whoosh_async (нужен route_project), _weighted_budgets (с v1.87.0 нужен лишь
# start_task — бюджет превью search, где он был общим, удалён), _cut_section_body
# (нужен handlers_articles), START_BLOCK_FLOOR (дефолт _weighted_budgets).
from memory_compiler.handlers_sessions import (  # noqa: E402,F401
    save_session, first_touch_context, session_note, open_questions,
    close_question, load_session, get_active_context, start_task, finish_task,
    _journal_gap_hint, _Block, _render_block,
    FIRST_TOUCH_CHARS, FIRST_TOUCH_QUESTIONS, SESSION_CHARS,
    SESSION_MAX_QUESTIONS, SESSION_Q_CHARS, START_BUDGET,
)
