"""Локализация описаний MCP-инструментов и промптов (MC_LANG).

Постобработка на выходе: tools.py с его 143 русскими описаниями не трогается —
тексты остаются рядом с инструментами, где их видно при чтении кода. Здесь лежит
только английский оверлей, который накладывается в list_tools()/list_prompts().

Границы (осознанные, см. спеку): ответы handlers.py остаются РУССКИМИ, поэтому при
MC_LANG=en клиент увидит английские описания и русский ответ. Лингвистические правила
(regex автотегирования в storage.py, стоп-слова в search.py) не трогаются вовсе —
это обработка русского контента базы, а не интерфейс.
"""
import os

# Дефолт ru — обратная совместимость. Сравнение строгое: любое неизвестное значение
# (fr, опечатка, пустая строка) даёт русский вывод, а не падение.
MC_LANG = os.environ.get("MC_LANG", "ru").lower()

# {имя инструмента: {"description": str, "params": {имя параметра: str}}}
TOOLS_EN: dict[str, dict] = {
    'save_lesson': {
        # RU: Сохранить или обновить статью в базе знаний. Автоматически находит существующую статью по теме и мержит новые факты.
        'description': 'Save or update an article in the knowledge base. Finds an existing article on the topic automatically and merges the new facts into it.',
        'params': {
            'topic': 'Short title',
            'content': 'Problem, cause, solution',
            'project': 'Project name',
            'force_new': 'Force creation of a new article',
        },
    },
    'get_context': {
        # RU: Получить контекст из базы знаний перед началом нетривиальной задачи.
        'description': 'Pull context from the knowledge base before starting a non-trivial task.',
        'params': {
            'project': 'Project name',
            'query': 'Task description',
        },
    },
    'search': {
        # RU: Найти похожие кейсы и решения в базе знаний.
        'description': 'Find similar cases and solutions in the knowledge base.',
        'params': {
            'query': 'Search query',
            'project': "Project name, or 'all'",
        },
    },
    'compile': {
        # RU: Скомпилировать daily логи в проектные статьи. Мержит записи в существующие статьи или создаёт новые. dry_run=true для превью.
        'description': 'Compile daily logs into project articles. Merges entries into existing articles or creates new ones. dry_run=true for a preview.',
        'params': {
            'dry_run': 'Preview without making changes',
            'project': 'Compile only entries for this project',
            'since': 'ISO date — process logs starting from this date',
        },
    },
    'lint': {
        # RU: Проверить здоровье базы знаний: дубли, устаревшее, пустые статьи, теги.
        'description': 'Check the health of the knowledge base: duplicates, stale content, empty articles, tags.',
        'params': {
            'project': "Project name, or 'all'",
            'fix': 'Auto-fix safe issues (tags, index)',
        },
    },
    'reindex': {
        # RU: Переиндексировать базу знаний (Whoosh BM25F + embeddings + index.md).
        'description': 'Reindex the knowledge base (Whoosh BM25F + embeddings + index.md).',
    },
    'save_session': {
        # RU: Сохранить контекст сессии (что сделано, что осталось, решения). Вызывать в конце сессии.
        'description': 'Save the session context (what was done, what remains, decisions). Call at the end of a session.',
        'params': {
            'project': 'Project name',
            'summary': 'What was done in this session',
            'decisions': 'Decisions made',
            'open_questions': 'What remains / open questions',
        },
    },
    'load_session': {
        # RU: Загрузить контекст предыдущей сессии. Вызывать в начале сессии.
        'description': 'Load the context of the previous session. Call at the start of a session.',
        'params': {
            'project': 'Project name',
        },
    },
    'get_summary': {
        # RU: Получить сжатую сводку проекта (заголовки, теги, ключевые факты). ~200 токенов.
        'description': 'Get a compact project summary (titles, tags, key facts). ~200 tokens.',
        'params': {
            'project': 'Project name',
        },
    },
    'ask': {
        # RU: Задать вопрос — получить ответ с цитатами из статей базы знаний.
        'description': 'Ask a question — get an answer with quotes from knowledge base articles.',
        'params': {
            'question': 'Question in natural language',
            'project': "Project name, or 'all'",
        },
    },
    'get_active_context': {
        # RU: Получить активный контекст проекта — последние 10 действий/решений.
        'description': 'Get the active context of a project — the last 10 actions/decisions.',
        'params': {
            'project': 'Project name',
        },
    },
    'delete_article': {
        # RU: Удалить статью из базы знаний.
        'description': 'Delete an article from the knowledge base.',
        'params': {
            'project': 'Project name',
            'filename': 'Article file name (e.g., my_article.md)',
        },
    },
    'edit_article': {
        # RU: Заменить содержимое статьи или добавить секцию.
        'description': "Replace an article's content or add a section.",
        'params': {
            'project': 'Project name',
            'filename': 'Article file name',
            'content': 'New content (full replacement of the article body)',
            'append': 'True — append to the end, False — replace the body',
        },
    },
    'context_gaps': {
        # RU: Выдать статьи, которым нужен ИИ-контекст секций (для генерации). Многосекционные не-секретные без contexts. Затем — save_contexts.
        'description': 'List articles that need AI-generated section context (for generation). Multi-section, non-secret, without contexts. Follow up with save_contexts.',
        'params': {
            'project': "Project, or 'all'",
            'limit': 'How many articles at a time',
        },
    },
    'save_contexts': {
        # RU: Сохранить ИИ-контексты секций во frontmatter статьи и ре-эмбеддить.
        'description': "Save AI-generated section contexts into the article's frontmatter and re-embed.",
        'params': {
            'project': 'Project name',
            'filename': 'Article file name',
            'contexts': 'List of {heading, context} per section',
        },
    },
    'read_article': {
        # RU: Получить полный текст статьи.
        'description': 'Get the full text of an article.',
        'params': {
            'project': "Project name, or 'daily'",
            'filename': 'Article file name',
        },
    },
    'search_by_tag': {
        # RU: Найти все статьи с указанным тегом.
        'description': 'Find all articles with the given tag.',
        'params': {
            'tag': 'Tag to search for',
            'project': "Project name, or 'all'",
        },
    },
    'backlinks': {
        # RU: Кто ссылается на статью: обратные РУЧНЫЕ связи ...
        'description': ("Who links to this article: incoming MANUAL links "
                        "([[wiki-links]] and markdown links in the body) with a context "
                        "line. The auto-generated «See also» block is excluded — "
                        "it reflects semantic similarity, which related already shows."),
        'params': {
            'project': "Article's project name",
            'filename': 'Article file name',
        },
    },
    'article_history': {
        # RU: Получить историю изменений статьи (git log).
        'description': "Get an article's change history (git log).",
        'params': {
            'project': 'Project name',
            'filename': 'Article file name',
        },
    },
    'init_schema': {
        # RU: Создать шаблон _schema.md в проекте — контракт сущностей/связей/стиля (Karpathy LLM Wiki pattern). Идемпотентно: не перезаписывает существующий.
        'description': 'Create a _schema.md template in the project — a contract for entities/relations/style (Karpathy LLM Wiki pattern). Idempotent: does not overwrite an existing one.',
        'params': {
            'project': 'Project name',
        },
    },
    'add_project': {
        # RU: Создать новый проект в базе знаний.
        'description': 'Create a new project in the knowledge base.',
        'params': {
            'name': 'Project name (Latin letters, no spaces)',
        },
    },
    'remove_project': {
        # RU: Удалить проект из базы знаний (все статьи проекта будут удалены). Требует confirm=true если в проекте есть статьи.
        'description': "Delete a project from the knowledge base (all of the project's articles will be deleted). Requires confirm=true if the project has articles.",
        'params': {
            'name': 'Name of the project to delete',
            'confirm': 'Confirmation of deletion (required if the project has articles)',
        },
    },
    'list_projects': {
        # RU: Список всех проектов с количеством статей.
        'description': 'List of all projects with article counts.',
    },
    'start_task': {
        # RU: Начать нетривиальную задачу. ВЫЗЫВАЙ ПЕРВЫМ ДЕЙСТВИЕМ при получении задачи (баг, доработка, настройка, интеграция, деплой). Ищет похожие кейсы + загружает контекст сессии.
        'description': 'Start a non-trivial task. CALL AS THE FIRST ACTION when given a task (bug, enhancement, setup, integration, deployment). Searches for similar cases + loads the session context.',
        'params': {
            'topic': 'Task topic — what needs to be done',
            'project': "Project name (if known, otherwise 'all')",
        },
    },
    'finish_task': {
        # RU: Завершить задачу и сохранить решение. ВЫЗЫВАЙ ПОСЛЕ РЕШЕНИЯ любой нетривиальной задачи. Сохраняет урок + контекст сессии.
        'description': 'Finish a task and save the solution. CALL AFTER SOLVING any non-trivial task. Saves the lesson + the session context.',
        'params': {
            'topic': 'Short title of the solved task',
            'content': 'Problem + solution + key facts',
            'project': 'Project name',
            'session_summary': 'What was done in the session',
            'open_questions': 'What remains / open questions',
        },
    },
    'search_snippets': {
        # RU: Поиск по кодовым блокам в статьях.
        'description': 'Search code blocks within articles.',
        'params': {
            'query': 'What to search for in the code',
            'lang': 'Language: python, bash, yaml, 1c, sql',
        },
    },
    'save_runbook': {
        # RU: Создать runbook — пошаговую инструкцию с чекбоксами.
        'description': 'Create a runbook — a step-by-step guide with checkboxes.',
        'params': {
            'topic': 'Runbook title',
            'steps': 'List of steps',
            'project': 'Project name',
        },
    },
    'get_runbook': {
        # RU: Получить runbook с прогрессом выполнения.
        'description': 'Get a runbook with its completion progress.',
        'params': {
            'project': 'Project name',
            'filename': 'Runbook filename',
        },
    },
    'search_error': {
        # RU: Поиск похожих ошибок в базе знаний. Принимает трейсбек или текст ошибки.
        'description': 'Search the knowledge base for similar errors. Accepts a traceback or error text.',
        'params': {
            'error_text': 'Traceback or error text',
        },
    },
    'set_project_deps': {
        # RU: Установить зависимости проекта. При start_task контекст подтягивается из зависимых проектов.
        'description': "Set a project's dependencies. During start_task, context is pulled in from dependent projects.",
        'params': {
            'project': 'Project name',
            'depends_on': 'List of dependency projects',
        },
    },
    'get_project_deps': {
        # RU: Получить зависимости проекта.
        'description': "Get a project's dependencies.",
        'params': {
            'project': 'Project name',
        },
    },
    'save_decision': {
        # RU: Записать архитектурное/техническое решение с обоснованием.
        'description': 'Record an architectural/technical decision with its rationale.',
        'params': {
            'title': 'Decision title',
            'decision': 'What was decided',
            'alternatives': 'What alternatives existed (optional; empty = none were considered)',
            'reasoning': 'Why this was chosen',
            'project': 'Project name',
        },
    },
    'search_decisions': {
        # RU: Поиск по журналу решений.
        'description': 'Search the decision log.',
        'params': {
            'query': 'Search query',
        },
    },
    'save_from_template': {
        # RU: Создать статью по шаблону (bug, setup, 1c, deploy, integration).
        'description': 'Create an article from a template (bug, setup, 1c, deploy, integration).',
        'params': {
            'template': 'Template name: bug, setup, 1c, deploy, integration',
            'fields': 'Template fields (depend on the type)',
            'project': 'Project name',
        },
    },
    'list_templates': {
        # RU: Список доступных шаблонов статей.
        'description': 'List of available article templates.',
    },
    'save_tracking': {
        # RU: Создать или обновить tracking-статью (снимок текущего состояния). Старое значение → history[], новое → current. Используй для 'текущая версия', 'текущий деплой' и т.д.
        'description': "Create or update a tracking article (a snapshot of the current state). The old value goes to history[], the new one becomes current. Use it for things like 'current version', 'current deployment', etc.",
        'params': {
            'project': 'Project name',
            'entity': 'Entity name: release, deployment, config',
            'facts': "Facts: {version: '1.3.50', url: ...}",
            'narrative': 'Optional description (auto-generated otherwise)',
        },
    },
    'get_current': {
        # RU: Получить текущее состояние из tracking-статьи.
        'description': 'Get the current state from a tracking article.',
        'params': {
            'project': 'Project name',
            'entity': 'Entity name: release, deployment, config',
        },
    },
    'consolidate': {
        # RU: Найти дубли/похожие статьи: near-exact детектор РЕАЛЬНЫХ дублей (точный/containment матч по тексту) + похожие темы по embeddings. НЕ мержит автоматически.
        'description': 'Find duplicate/similar articles: a near-exact detector for REAL duplicates (exact/containment text match) + similar topics via embeddings. Does NOT merge automatically.',
        'params': {
            'min_sim': 'Embedding-similarity threshold for "similar topics" (e5). 0.985 is near-duplicates; lower produces many false positives on a short RU corpus. Real duplicates are caught by near-exact, not by this threshold.',
        },
    },
    'save_compact': {
        # RU: Сохранить summary при сжатии контекста (PostCompact event). Записывает в _compact_history.md проекта (FIFO 5). Подтягивается в start_task — даёт continuous memory через compact-границы. Используй когда контекст сжимается и важно сохранить контекст работы.
        'description': "Save a summary when the context is compacted (PostCompact event). Writes to the project's _compact_history.md (FIFO 5). Pulled into start_task — provides continuous memory across compact boundaries. Use when the context is being compacted and it's important to preserve the working context.",
        'params': {
            'project': 'Project name',
            'summary': 'Brief summary of what happened before the compaction (what was being done, key decisions, open questions)',
        },
    },
    'stale_facts': {
        # RU: Stale fact watcher — найти статьи с устаревающими фактами: SSL-сертификаты с близким expiration, истёкшие, секреты/cert старше 180 дней. Источники: regex 'valid until / до DATE' в тексте, tracking-frontmatter (current.until/expires), теги ssl/cert/password/license + age статьи.
        'description': "Stale fact watcher — find articles with facts that are going stale: SSL certificates nearing expiration, expired ones, secrets/certs older than 180 days. Sources: a regex for date phrases like 'valid until DATE' (English or Russian phrasing) in the text, tracking frontmatter (current.until/expires), ssl/cert/password/license tags + article age.",
        'params': {
            'warn_days': 'How many days ahead to warn',
        },
    },
    'gap_report': {
        # RU: Knowledge gap report — что чаще всего ищут но не находят. Анализирует audit-лог: запросы с пустым / слабым результатом (top_score<35), топ-темы по частоте, проекты-сироты (≤2 статей).
        'description': 'Knowledge gap report — what is searched for most often but not found. Analyzes the audit log: queries with an empty / weak result (top_score<35), top topics by frequency, orphan projects (≤2 articles).',
        'params': {
            'project': "Filter by project, 'all' = all",
            'days': 'Analysis window in days',
            'limit': 'Top-N in each section',
        },
    },
    'route_project': {
        # RU: Авто-определение лучшего проекта. Передай cwd (рабочий каталог) И/ИЛИ text (описание задачи). Если cwd содержит имя существующего проекта — используется СРАЗУ (override). Иначе ранжирует через substring/token/content match.
        'description': 'Auto-detect the best project. Pass cwd (working directory) AND/OR text (task description). If cwd contains the name of an existing project, it is used IMMEDIATELY (override). Otherwise it ranks candidates via substring/token/content match.',
        'params': {
            'text': 'Query/task description/mentioned entity (optional)',
            'cwd': "Client's current working directory (a STRONG signal; if it contains a project name, it's used as an override)",
            'top_k': 'How many candidates to return (default 3)',
        },
    },
    'save_secret': {
        # RU: Сохранить зашифрованную секретную статью (пароли, ключи, credentials).
        'description': 'Save an encrypted secret article (passwords, keys, credentials).',
        'params': {
            'topic': 'Secret title',
            'content': 'Content (will be encrypted)',
            'project': 'Project name',
        },
    },
    'git_capture': {
        # RU: Автосбор знаний из git-коммитов. Два режима: repo_path (сервер читает git log из смонтированного репо) или git_log_raw (клиент передаёт вывод 'git log --format="%H|%s|%an|%aI" --numstat').
        'description': 'Auto-collect knowledge from git commits. Two modes: repo_path (the server reads git log from a mounted repo) or git_log_raw (the client passes the output of \'git log --format="%H|%s|%an|%aI" --numstat\').',
        'params': {
            'repo_path': 'Path to the git repository (on the server/in the container)',
            'project': 'Project in the KB to save into',
            'since': "Starting point: ISO date, '3 days ago', commit hash. Default: since the last capture",
            'auto_save': 'true = save as articles, false = return a summary for review',
            'group_by': 'Grouping: prefix (conventional commits), branch, file (by directory)',
            'git_log_raw': "Raw git log output (instead of repo_path). Format: git log --format='%H|%s|%an|%aI' --numstat",
        },
    },
    'import_obsidian': {
        # RU: Импорт заметок из Obsidian vault. Парсит YAML frontmatter, теги, wiki-ссылки. dry_run=true для превью.
        'description': 'Import notes from an Obsidian vault. Parses YAML frontmatter, tags, wiki-links. dry_run=true for a preview.',
        'params': {
            'vault_path': 'Path to the Obsidian vault',
            'project': 'Target project in the KB (default for all notes)',
            'folder_mapping': 'Mapping of vault folders → KB projects. For example: {"Work": "work", "Infrastructure": "infra"}',
            'dry_run': 'true = preview, false = import',
            'skip_inbox': 'Skip the Inbox folder',
        },
    },
    'knowledge_gap': {
        # RU: Найти темы активные в git-коммитах, но отсутствующие в базе знаний. Полезно для обнаружения недокументированных знаний.
        'description': 'Find topics active in git commits but missing from the knowledge base. Useful for discovering undocumented knowledge.',
        'params': {
            'repo_path': 'Path to the git repository',
            'project': "Project to compare against (or 'all')",
            'days': 'How many recent days of commits to analyze',
            'git_log_raw': 'Raw git log (alternative to repo_path)',
        },
    },
    'ingest': {
        # RU: Загрузить знания из внешнего источника (URL или текст). Два режима: url (сервер загружает страницу, конвертирует HTML→markdown) или raw_text (клиент передаёт текст из PDF/документа).
        'description': 'Load knowledge from an external source (URL or text). Two modes: url (the server fetches the page, converts HTML→markdown) or raw_text (the client passes text from a PDF/document).',
        'params': {
            'project': 'Project in the KB to save into',
            'url': 'URL of the web page to load',
            'raw_text': 'Ready-made text (instead of url). For PDFs, documents, etc.',
            'source': 'Description of the source (for raw_text): file name, URL, etc.',
            'topic': 'Article topic (default: the page title)',
            'auto_save': 'true = save immediately, false = preview',
        },
    },
}

# {имя промпта: {"title": str, "description": str, "args": {имя аргумента: str}}}
PROMPTS_EN: dict[str, dict] = {
    'load-context': {
        # RU title: Загрузить контекст проекта
        'title': 'Load project context',
        # RU: Поднять рабочий контекст проекта из базы знаний (активный контекст, решения, открытые вопросы).
        'description': 'Pull the working context of a project from the knowledge base (active context, decisions, open questions).',
        'args': {'project': 'Project name'},
    },
    'save-session': {
        # RU title: Сохранить сессию
        'title': 'Save session',
        # RU: Сохранить итог текущей сессии по проекту (что сделано, решения, что осталось).
        'description': 'Save the outcome of the current session for a project (what was done, decisions, what is left).',
        'args': {'project': 'Project name'},
    },
    'save-lesson': {
        # RU title: Сохранить урок
        'title': 'Save lesson',
        # RU: Сформулировать и сохранить урок (проблема → причина → решение → факты) в проект.
        'description': 'Formulate and save a lesson (problem → cause → solution → facts) to a project.',
        'args': {
            'project': 'Project name',
            'topic': 'Lesson topic (optional)',
        },
    },
    'weekly-review': {
        # RU title: Еженедельный обзор
        'title': 'Weekly review',
        # RU: Свести из базы знаний последние решения, изменения статусов, открытые вопросы и knowledge gaps.
        'description': 'Pull together the latest decisions, status changes, open questions, and knowledge gaps from the knowledge base.',
        'args': {'project': 'Project name (optional; otherwise all)'},
    },
}


def localize_tools(tools):
    """English tool descriptions when MC_LANG=en, otherwise the input unchanged.

    Returns COPIES: list_tools() builds the objects anew each call, but that isn't
    something to rely on — mutating someone else's objects would make language-switch
    tests unreliable.
    """
    if MC_LANG != "en":
        return tools
    out = []
    for tool in tools:
        entry = TOOLS_EN.get(tool.name)
        if not entry:
            out.append(tool)  # перевода нет — отдаём русский, сервер не падает
            continue
        copy = tool.model_copy(deep=True)
        if entry.get("description"):
            copy.description = entry["description"]
        props = (copy.inputSchema or {}).get("properties", {})
        for param, text in entry.get("params", {}).items():
            if param in props:
                props[param]["description"] = text
        out.append(copy)
    return out


def localize_prompts(prompts):
    """Same for prompts. Translates title, description, and argument descriptions.

    ⚠️ list_prompts() returns the MODULE-LEVEL CONSTANT _PROMPTS — mutating it in
    place would corrupt it for the rest of the process.
    """
    if MC_LANG != "en":
        return prompts
    out = []
    for prompt in prompts:
        entry = PROMPTS_EN.get(prompt.name)
        if not entry:
            out.append(prompt)
            continue
        copy = prompt.model_copy(deep=True)
        if entry.get("title"):
            copy.title = entry["title"]
        if entry.get("description"):
            copy.description = entry["description"]
        for arg in copy.arguments or []:
            text = entry.get("args", {}).get(arg.name)
            if text:
                arg.description = text
        out.append(copy)
    return out
