"""MCP tool definitions and dispatch."""
from mcp.server import Server
from mcp.types import Tool, TextContent

from memory_compiler.config import PROJECTS, stats
from memory_compiler.search import rebuild_index, rebuild_embeddings
from memory_compiler.storage import regenerate_index
from memory_compiler import handlers

app = Server("memory-compiler")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="save_lesson",
            description="Сохранить или обновить статью в базе знаний. Автоматически находит существующую статью по теме и мержит новые факты.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Короткое название"},
                    "content": {"type": "string", "description": "Проблема, причина, решение"},
                    "project": {"type": "string", "description": "Имя проекта"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "force_new": {"type": "boolean", "default": False, "description": "Принудительно создать новую статью"}
                },
                "required": ["topic", "content", "project"]
            }
        ),
        Tool(
            name="get_context",
            description="Получить контекст из базы знаний перед началом нетривиальной задачи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "query": {"type": "string", "description": "Описание задачи"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="search",
            description="Найти похожие кейсы и решения в базе знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string", "default": "all", "description": "Имя проекта или 'all'"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="compile",
            description="Скомпилировать daily логи в проектные статьи. Мержит записи в существующие статьи или создаёт новые. dry_run=true для превью.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "default": True, "description": "Превью без изменений"},
                    "project": {"type": "string", "enum": PROJECTS + ["all"], "description": "Компилировать только записи этого проекта"},
                    "since": {"type": "string", "description": "ISO дата — обрабатывать логи начиная с этой даты"}
                }
            }
        ),
        Tool(
            name="lint",
            description="Проверить здоровье базы знаний: дубли, устаревшее, пустые статьи, теги.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "all", "description": "Имя проекта или 'all'"},
                    "fix": {"type": "boolean", "default": False, "description": "Автоисправление безопасных проблем (теги, index)"}
                }
            }
        ),
        Tool(
            name="reindex",
            description="Переиндексировать базу знаний (Whoosh BM25F + embeddings + index.md).",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="save_session",
            description="Сохранить контекст сессии (что сделано, что осталось, решения). Вызывать в конце сессии.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "summary": {"type": "string", "description": "Что сделано в этой сессии"},
                    "decisions": {"type": "string", "description": "Принятые решения"},
                    "open_questions": {"type": "string", "description": "Что осталось / открытые вопросы"}
                },
                "required": ["project", "summary"]
            }
        ),
        Tool(
            name="load_session",
            description="Загрузить контекст предыдущей сессии. Вызывать в начале сессии.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="get_summary",
            description="Получить сжатую сводку проекта (заголовки, теги, ключевые факты). ~200 токенов.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="ask",
            description="Задать вопрос — получить ответ с цитатами из статей базы знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Вопрос на естественном языке"},
                    "project": {"type": "string", "default": "all", "description": "Имя проекта или 'all'"}
                },
                "required": ["question"]
            }
        ),
        Tool(
            name="get_active_context",
            description="Получить активный контекст проекта — последние 10 действий/решений.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="delete_article",
            description="Удалить статью из базы знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "filename": {"type": "string", "description": "Имя файла статьи (например, my_article.md)"}
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="edit_article",
            description="Заменить содержимое статьи или добавить секцию.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "filename": {"type": "string", "description": "Имя файла статьи"},
                    "content": {"type": "string", "description": "Новое содержимое (полная замена тела статьи)"},
                    "append": {"type": "boolean", "default": False, "description": "True — дописать в конец, False — заменить тело"}
                },
                "required": ["project", "filename", "content"]
            }
        ),
        Tool(
            name="read_article",
            description="Получить полный текст статьи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта или 'daily'"},
                    "filename": {"type": "string", "description": "Имя файла статьи"}
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="search_by_tag",
            description="Найти все статьи с указанным тегом.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Тег для поиска"},
                    "project": {"type": "string", "default": "all", "description": "Имя проекта или 'all'"}
                },
                "required": ["tag"]
            }
        ),
        Tool(
            name="article_history",
            description="Получить историю изменений статьи (git log).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "filename": {"type": "string", "description": "Имя файла статьи"}
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="add_project",
            description="Создать новый проект в базе знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя проекта (латиница, без пробелов)"}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="remove_project",
            description="Удалить проект из базы знаний (все статьи проекта будут удалены).",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя проекта для удаления"}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="list_projects",
            description="Список всех проектов с количеством статей.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="start_task",
            description="Начать нетривиальную задачу. ВЫЗЫВАЙ ПЕРВЫМ ДЕЙСТВИЕМ при получении задачи (баг, доработка, настройка, интеграция, деплой). Ищет похожие кейсы + загружает контекст сессии.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Тема задачи — что нужно сделать"},
                    "project": {"type": "string", "description": "Имя проекта (если известно, иначе 'all')"}
                },
                "required": ["topic"]
            }
        ),
        Tool(
            name="finish_task",
            description="Завершить задачу и сохранить решение. ВЫЗЫВАЙ ПОСЛЕ РЕШЕНИЯ любой нетривиальной задачи. Сохраняет урок + контекст сессии.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Краткое название решённой задачи"},
                    "content": {"type": "string", "description": "Проблема + решение + ключевые факты"},
                    "project": {"type": "string", "description": "Имя проекта"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "session_summary": {"type": "string", "description": "Что сделано в сессии"},
                    "open_questions": {"type": "string", "description": "Что осталось / открытые вопросы"}
                },
                "required": ["topic", "content", "project"]
            }
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name in stats:
        stats[name] = stats.get(name, 0) + 1
    if name == "save_lesson":
        result = await handlers.save_lesson(**arguments)
    elif name == "get_context":
        result = await handlers.get_context(**arguments)
    elif name == "search":
        result = await handlers.search(**arguments)
    elif name == "compile":
        result = await handlers.compile(arguments.get("dry_run", True), arguments.get("project"), arguments.get("since"))
    elif name == "lint":
        result = await handlers.lint(arguments.get("project", "all"), arguments.get("fix", False))
    elif name == "reindex":
        count = rebuild_index()
        ecount = rebuild_embeddings()
        regenerate_index()
        result = [TextContent(type="text", text=f"\u2705 \u041f\u0435\u0440\u0435\u0438\u043d\u0434\u0435\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u043e: {count} \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u043e\u0432 (BM25F + {ecount} embeddings), index.md \u043e\u0431\u043d\u043e\u0432\u043b\u0451\u043d")]
    elif name == "save_session":
        result = await handlers.save_session(**arguments)
    elif name == "load_session":
        result = await handlers.load_session(**arguments)
    elif name == "get_summary":
        result = await handlers.get_summary(**arguments)
    elif name == "ask":
        result = await handlers.ask(**arguments)
    elif name == "get_active_context":
        result = await handlers.get_active_context(**arguments)
    elif name == "delete_article":
        result = await handlers.delete_article(**arguments)
    elif name == "edit_article":
        result = await handlers.edit_article(**arguments)
    elif name == "read_article":
        result = await handlers.read_article(**arguments)
    elif name == "search_by_tag":
        result = await handlers.search_by_tag(**arguments)
    elif name == "article_history":
        result = await handlers.article_history(**arguments)
    elif name == "add_project":
        result = await handlers.add_project(**arguments)
    elif name == "remove_project":
        result = await handlers.remove_project(**arguments)
    elif name == "list_projects":
        result = await handlers.list_projects()
    elif name == "start_task":
        result = await handlers.start_task(**arguments)
    elif name == "finish_task":
        result = await handlers.finish_task(**arguments)
    else:
        result = [TextContent(type="text", text=f"\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 \u0438\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d\u0442: {name}")]
    # Track response size
    total = sum(len(t.text) for t in result)
    stats["total_chars_returned"] = stats.get("total_chars_returned", 0) + total
    return result
