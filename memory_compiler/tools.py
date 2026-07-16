"""MCP tool definitions and dispatch."""
import time

from mcp.server import Server
from mcp.types import (
    Tool, TextContent, ToolAnnotations, Resource, ResourceTemplate,
    Prompt, PromptArgument, PromptMessage, GetPromptResult, Completion,
)
from mcp.server.lowlevel.helper_types import ReadResourceContents

from memory_compiler import config
from memory_compiler.config import PROJECTS, stats
from memory_compiler.search import rebuild_index, rebuild_embeddings, start_background_reindex
from memory_compiler.storage import regenerate_index, audit_log
from memory_compiler import handlers
from memory_compiler import obs

app = Server("memory-compiler")


# --- Tool annotations (MCP hints для клиента, напр. Claude Desktop) ---------
# Классификация статична (per tool). Принцип: «может мутировать» => readOnlyHint=False,
# даже если дефолтные аргументы читают (lint fix=False, compile dry_run=True) — иначе
# клиент авто-подтвердит потенциально пишущий вызов.
_READONLY_LOCAL = frozenset({
    "get_context", "search", "load_session", "get_summary", "ask",
    "get_active_context", "read_article", "search_by_tag", "article_history",
    "list_projects", "search_snippets", "get_runbook", "search_error",
    "get_project_deps", "search_decisions", "list_templates", "get_current",
    "consolidate", "stale_facts", "gap_report", "route_project",
})
# read-only, но читает внешний источник (git-репо/лог) — openWorld
_READONLY_OPENWORLD = frozenset({"knowledge_gap"})
# необратимое удаление данных
_DESTRUCTIVE = frozenset({"delete_article", "remove_project"})
# повторный вызов с теми же аргументами не даёт доп. эффекта
_IDEMPOTENT_WRITE = frozenset({"reindex", "init_schema"})
# пишет в базу И тянет внешний источник (URL/vault/git)
_OPENWORLD_WRITE = frozenset({"ingest", "import_obsidian", "git_capture"})


def _annotations_for(name: str) -> ToolAnnotations:
    """Вернуть ToolAnnotations по имени tool. По умолчанию — локальная не-деструктивная запись."""
    if name in _READONLY_LOCAL:
        return ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    if name in _READONLY_OPENWORLD:
        return ToolAnnotations(readOnlyHint=True, openWorldHint=True)
    if name in _DESTRUCTIVE:
        return ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    if name in _IDEMPOTENT_WRITE:
        return ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=False)
    if name in _OPENWORLD_WRITE:
        return ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    return ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


@app.list_tools()
async def list_tools() -> list[Tool]:
    tools = [
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
            },
            # Машиночитаемая выдача (structuredContent) для программных клиентов —
            # список найденных статей с URI-ресурсами. Человекочитаемый текст + resource
            # links остаются в content. Схема нестрогая (additionalProperties по умолчанию).
            outputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer"},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "uri": {"type": "string", "description": "memory://<проект>/<файл>"},
                                "name": {"type": "string"},
                                "title": {"type": "string"},
                                "score": {"type": "string"}
                            },
                            "required": ["uri", "name"]
                        }
                    }
                },
                "required": ["query", "count", "results"]
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
            name="init_schema",
            description="Создать шаблон _schema.md в проекте — контракт сущностей/связей/стиля (Karpathy LLM Wiki pattern). Идемпотентно: не перезаписывает существующий.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"}
                },
                "required": ["project"]
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
            description="Удалить проект из базы знаний (все статьи проекта будут удалены). Требует confirm=true если в проекте есть статьи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя проекта для удаления"},
                    "confirm": {"type": "boolean", "default": False, "description": "Подтверждение удаления (обязательно если в проекте есть статьи)"}
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
        Tool(
            name="search_snippets",
            description="Поиск по кодовым блокам в статьях.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Что искать в коде"},
                    "lang": {"type": "string", "description": "Язык: python, bash, yaml, 1c, sql"},
                    "project": {"type": "string", "default": "all"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="save_runbook",
            description="Создать runbook — пошаговую инструкцию с чекбоксами.",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Название runbook"},
                    "steps": {"type": "array", "items": {"type": "string"}, "description": "Список шагов"},
                    "project": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["topic", "steps", "project"]
            }
        ),
        Tool(
            name="get_runbook",
            description="Получить runbook с прогрессом выполнения.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "filename": {"type": "string"}
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="search_error",
            description="Поиск похожих ошибок в базе знаний. Принимает трейсбек или текст ошибки.",
            inputSchema={
                "type": "object",
                "properties": {
                    "error_text": {"type": "string", "description": "Трейсбек или текст ошибки"},
                    "project": {"type": "string", "default": "all"}
                },
                "required": ["error_text"]
            }
        ),
        Tool(
            name="set_project_deps",
            description="Установить зависимости проекта. При start_task контекст подтягивается из зависимых проектов.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Список проектов-зависимостей"}
                },
                "required": ["project", "depends_on"]
            }
        ),
        Tool(
            name="get_project_deps",
            description="Получить зависимости проекта.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="save_decision",
            description="Записать архитектурное/техническое решение с обоснованием.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Название решения"},
                    "decision": {"type": "string", "description": "Что решили"},
                    "alternatives": {"type": "string", "description": "Какие были альтернативы"},
                    "reasoning": {"type": "string", "description": "Почему выбрали это"},
                    "project": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["title", "decision", "alternatives", "reasoning", "project"]
            }
        ),
        Tool(
            name="search_decisions",
            description="Поиск по журналу решений.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string", "default": "all"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="save_from_template",
            description="Создать статью по шаблону (bug, setup, 1c, deploy, integration).",
            inputSchema={
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "Имя шаблона: bug, setup, 1c, deploy, integration"},
                    "fields": {"type": "object", "description": "Поля шаблона (зависят от типа)"},
                    "project": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["template", "fields", "project"]
            }
        ),
        Tool(
            name="list_templates",
            description="Список доступных шаблонов статей.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="save_tracking",
            description="Создать или обновить tracking-статью (снимок текущего состояния). Старое значение → history[], новое → current. Используй для 'текущая версия', 'текущий деплой' и т.д.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "entity": {"type": "string", "description": "Название сущности: release, deployment, config"},
                    "facts": {"type": "object", "description": "Факты: {version: '1.3.50', url: ...}"},
                    "narrative": {"type": "string", "description": "Опциональное описание (иначе автогенерация)"}
                },
                "required": ["project", "entity", "facts"]
            }
        ),
        Tool(
            name="get_current",
            description="Получить текущее состояние из tracking-статьи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "entity": {"type": "string"}
                },
                "required": ["project", "entity"]
            }
        ),
        Tool(
            name="consolidate",
            description="Найти семантически похожие статьи в проекте — кандидаты на слияние. Использует embeddings (cosine similarity). НЕ мержит автоматически — возвращает список пар для ручной проверки.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "all"},
                    "min_sim": {"type": "number", "default": 0.90, "description": "Порог similarity (e5 с префиксами). 0.90 — близкие; 0.95+ — почти дубли"}
                },
                "required": []
            }
        ),
        Tool(
            name="save_compact",
            description="Сохранить summary при сжатии контекста (PostCompact event). Записывает в _compact_history.md проекта (FIFO 5). Подтягивается в start_task — даёт continuous memory через compact-границы. Используй когда контекст сжимается и важно сохранить контекст работы.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "summary": {"type": "string", "description": "Краткое резюме того что было до сжатия (что делали, ключевые решения, открытые вопросы)"}
                },
                "required": ["project", "summary"]
            }
        ),
        Tool(
            name="stale_facts",
            description="Stale fact watcher — найти статьи с устаревающими фактами: SSL-сертификаты с близким expiration, истёкшие, секреты/cert старше 180 дней. Источники: regex 'valid until / до DATE' в тексте, tracking-frontmatter (current.until/expires), теги ssl/cert/password/license + age статьи.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "all"},
                    "warn_days": {"type": "integer", "default": 30, "description": "За сколько дней предупреждать"}
                },
                "required": []
            }
        ),
        Tool(
            name="gap_report",
            description="Knowledge gap report — что чаще всего ищут но не находят. Анализирует audit-лог: запросы с пустым / слабым результатом (top_score<35), топ-темы по частоте, проекты-сироты (≤2 статей).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "all", "description": "Фильтр по проекту, 'all' = все"},
                    "days": {"type": "integer", "default": 30, "description": "Окно анализа в днях"},
                    "limit": {"type": "integer", "default": 10, "description": "Top-N в каждой секции"}
                },
                "required": []
            }
        ),
        Tool(
            name="route_project",
            description="Авто-определение лучшего проекта. Передай cwd (рабочий каталог) И/ИЛИ text (описание задачи). Если cwd содержит имя существующего проекта — используется СРАЗУ (override). Иначе ранжирует через substring/token/content match.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Запрос/описание задачи/упоминаемая сущность (опционально)"},
                    "cwd": {"type": "string", "description": "Текущий рабочий каталог клиента (СИЛЬНЫЙ сигнал; если содержит имя проекта — используется как override)"},
                    "top_k": {"type": "integer", "default": 3, "description": "Сколько кандидатов вернуть (default 3)"}
                },
                "required": []
            }
        ),
        Tool(
            name="save_secret",
            description="Сохранить зашифрованную секретную статью (пароли, ключи, credentials).",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Название секрета"},
                    "content": {"type": "string", "description": "Содержание (будет зашифровано)"},
                    "project": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["topic", "content", "project"]
            }
        ),
        Tool(
            name="git_capture",
            description="Автосбор знаний из git-коммитов. Два режима: repo_path (сервер читает git log из смонтированного репо) или git_log_raw (клиент передаёт вывод 'git log --format=\"%H|%s|%an|%aI\" --numstat').",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Путь к git-репозиторию (на сервере/в контейнере)"},
                    "project": {"type": "string", "description": "Проект в KB для сохранения"},
                    "since": {"type": "string", "description": "С какого момента: дата ISO, '3 days ago', commit hash. По умолчанию: с последнего capture"},
                    "auto_save": {"type": "boolean", "default": False, "description": "true = сохранить как статьи, false = вернуть сводку для ревью"},
                    "group_by": {"type": "string", "enum": ["prefix", "branch", "file"], "default": "prefix", "description": "Группировка: prefix (conventional commits), branch, file (по директории)"},
                    "git_log_raw": {"type": "string", "description": "Сырой вывод git log (вместо repo_path). Формат: git log --format='%H|%s|%an|%aI' --numstat"}
                },
                "required": ["project"]
            }
        ),
        Tool(
            name="import_obsidian",
            description="Импорт заметок из Obsidian vault. Парсит YAML frontmatter, теги, wiki-ссылки. dry_run=true для превью.",
            inputSchema={
                "type": "object",
                "properties": {
                    "vault_path": {"type": "string", "description": "Путь к Obsidian vault"},
                    "project": {"type": "string", "description": "Целевой проект в KB (по умолчанию для всех заметок)"},
                    "folder_mapping": {"type": "object", "description": "Маппинг папок vault → проекты KB. Например: {\"Работа\": \"work\", \"Инфраструктура\": \"infra\"}"},
                    "dry_run": {"type": "boolean", "default": True, "description": "true = превью, false = импорт"},
                    "skip_inbox": {"type": "boolean", "default": True, "description": "Пропустить папку Inbox"}
                },
                "required": ["vault_path", "project"]
            }
        ),
        Tool(
            name="knowledge_gap",
            description="Найти темы активные в git-коммитах, но отсутствующие в базе знаний. Полезно для обнаружения недокументированных знаний.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Путь к git-репозиторию"},
                    "project": {"type": "string", "default": "all", "description": "Проект для сравнения (или 'all')"},
                    "days": {"type": "number", "default": 30, "description": "За сколько последних дней анализировать коммиты"},
                    "git_log_raw": {"type": "string", "description": "Сырой git log (альтернатива repo_path)"}
                }
            }
        ),
        Tool(
            name="ingest",
            description="Загрузить знания из внешнего источника (URL или текст). Два режима: url (сервер загружает страницу, конвертирует HTML→markdown) или raw_text (клиент передаёт текст из PDF/документа).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Проект в KB для сохранения"},
                    "url": {"type": "string", "description": "URL веб-страницы для загрузки"},
                    "raw_text": {"type": "string", "description": "Готовый текст (вместо url). Для PDF, документов и т.д."},
                    "source": {"type": "string", "description": "Описание источника (для raw_text): имя файла, URL и т.д."},
                    "topic": {"type": "string", "description": "Тема статьи (по умолчанию: заголовок страницы)"},
                    "auto_save": {"type": "boolean", "default": False, "description": "true = сохранить сразу, false = превью"}
                },
                "required": ["project"]
            }
        ),
    ]
    for t in tools:
        t.annotations = _annotations_for(t.name)
    return tools


# --- Resources (P1): статьи базы как memory://<проект>/<файл> ----------------
# База становится first-class контекстом: клиент (Claude Desktop) листает и
# @-упоминает статьи без tool-вызова. Секреты не отдаются: secret_*.md и статьи
# с SECRET_FLAG исключаются из листинга; read_resource редактирует инлайн ENC:.
_RESOURCE_MIME = "text/markdown"
_RESOURCE_SCHEME = "memory://"


def _is_meta_file(name: str) -> bool:
    """Служебные/не-статейные файлы, которые не показываем как ресурсы."""
    return (
        name.startswith("secret_")
        or name.startswith("_")
        or name.startswith(".")
        or name == "index.md"
        or not name.endswith(".md")
    )


def _resource_title(text: str, filename: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:120]
        if s and not s.startswith("---"):
            break
    return filename[:-3] if filename.endswith(".md") else filename


def _resource_description(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("---") and not s.startswith("**"):
            return s[:160]
    return ""


@app.list_resources()
async def list_resources() -> list[Resource]:
    from memory_compiler.storage import is_secret_article

    kd = config.KNOWLEDGE_DIR
    out: list[Resource] = []
    if not kd or not kd.exists():
        return out
    for proj_dir in sorted(kd.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith(".") or proj_dir.name == "daily":
            continue
        project = proj_dir.name
        for art in sorted(proj_dir.glob("*.md")):
            if _is_meta_file(art.name):
                continue
            try:
                text = art.read_text(encoding="utf-8")
            except Exception:
                continue
            if is_secret_article(text, art.name):
                continue  # секрет с SECRET_FLAG без префикса secret_
            out.append(Resource(
                uri=f"{_RESOURCE_SCHEME}{project}/{art.name}",
                name=f"{project}/{art.name}",
                title=_resource_title(text, art.name),
                description=_resource_description(text),
                mimeType=_RESOURCE_MIME,
                size=art.stat().st_size,
            ))
    return out


@app.read_resource()
async def read_resource(uri) -> list[ReadResourceContents]:
    from memory_compiler.storage import safe_article_path, is_secret_article, is_encrypted

    def notice(msg: str) -> list[ReadResourceContents]:
        return [ReadResourceContents(content=msg, mime_type=_RESOURCE_MIME)]

    uri_s = str(uri)
    if not uri_s.startswith(_RESOURCE_SCHEME):
        return notice(f"❌ Неподдерживаемый URI: {uri_s}")
    rest = uri_s[len(_RESOURCE_SCHEME):]
    if "/" not in rest:
        return notice(f"❌ Ожидается memory://<проект>/<файл>, получено: {uri_s}")
    project, filename = rest.split("/", 1)
    # AnyUrl percent-энкодит не-ASCII (кириллица) — раскодируем обратно в имя файла.
    from urllib.parse import unquote
    project, filename = unquote(project), unquote(filename)
    try:
        fpath = safe_article_path(project, filename)
    except ValueError as e:
        return notice(f"❌ Небезопасный путь: {e}")
    if not fpath.exists():
        return notice(f"Статья не найдена: {project}/{filename}")
    text = fpath.read_text(encoding="utf-8")
    if is_secret_article(text, filename):
        return notice("🔒 Это секретная статья — недоступна как ресурс. "
                      "Читай её через tool read_article (с расшифровкой) при необходимости.")
    # Редактируем инлайн-ENC: фрагменты — НЕ расшифровываем в пассивный контекст.
    if "ENC:" in text:
        text = "\n".join(
            "[зашифрованный фрагмент опущен]" if is_encrypted(line) else line
            for line in text.splitlines()
        )
    return [ReadResourceContents(content=text, mime_type=_RESOURCE_MIME)]


@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    return [ResourceTemplate(
        uriTemplate=_RESOURCE_SCHEME + "{project}/{filename}",
        name="knowledge-article",
        title="Статья базы знаний",
        description="Статья базы знаний по имени проекта и файла (например, memory://infra/nginx_setup.md). "
                    "Секретные статьи недоступны как ресурсы.",
        mimeType=_RESOURCE_MIME,
    )]


# --- Prompts (P1): нативные слэш-команды для клиента ------------------------
# В Claude Desktop появляются /mcp__memory-compiler__load-context, save-session,
# save-lesson, weekly-review — часть workflow memory-autopilot как нативные команды.
# Промпты отдают шаблонные сообщения-инструкции (не исполняют tools сами).
_PROMPTS: list[Prompt] = [
    Prompt(
        name="load-context",
        title="Загрузить контекст проекта",
        description="Поднять рабочий контекст проекта из базы знаний (активный контекст, решения, открытые вопросы).",
        arguments=[PromptArgument(name="project", description="Имя проекта", required=True)],
    ),
    Prompt(
        name="save-session",
        title="Сохранить сессию",
        description="Сохранить итог текущей сессии по проекту (что сделано, решения, что осталось).",
        arguments=[PromptArgument(name="project", description="Имя проекта", required=True)],
    ),
    Prompt(
        name="save-lesson",
        title="Сохранить урок",
        description="Сформулировать и сохранить урок (проблема → причина → решение → факты) в проект.",
        arguments=[
            PromptArgument(name="project", description="Имя проекта", required=True),
            PromptArgument(name="topic", description="Тема урока (опционально)", required=False),
        ],
    ),
    Prompt(
        name="weekly-review",
        title="Еженедельный обзор",
        description="Свести из базы знаний последние решения, изменения статусов, открытые вопросы и knowledge gaps.",
        arguments=[PromptArgument(name="project", description="Имя проекта (опционально; иначе все)", required=False)],
    ),
]


def _user_msg(text: str) -> PromptMessage:
    return PromptMessage(role="user", content=TextContent(type="text", text=text))


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return _PROMPTS


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
    args = arguments or {}
    project = (args.get("project") or "").strip()
    topic = (args.get("topic") or "").strip()

    if name == "load-context":
        p = project or "нужный проект"
        msg = (f"Подними рабочий контекст проекта «{p}» из базы знаний memory-compiler: "
               f"вызови start_task с темой «продолжение работы» (project={p}), затем покажи активный "
               f"контекст, последние решения и открытые вопросы. Кратко резюмируй, на чём остановились.")
        return GetPromptResult(description=f"Загрузка контекста проекта {p}", messages=[_user_msg(msg)])

    if name == "save-session":
        p = project or "текущий проект"
        msg = (f"Сохрани итог текущей сессии по проекту «{p}»: вызови save_session (project={p}) с кратким "
               f"summary сделанного, принятыми решениями и открытыми вопросами. Если решалась нетривиальная "
               f"задача — дополнительно finish_task с проблемой, причиной, решением и ключевыми фактами.")
        return GetPromptResult(description=f"Сохранение сессии проекта {p}", messages=[_user_msg(msg)])

    if name == "save-lesson":
        p = project or "нужный проект"
        about = f" про «{topic}»" if topic else ""
        msg = (f"Сохрани урок{about} в проект «{p}»: сформулируй проблему, причину, решение и ключевые факты, "
               f"затем вызови save_lesson (project={p}). Если это был выбор между альтернативами — save_decision; "
               f"если пошаговая инструкция — save_runbook.")
        return GetPromptResult(description=f"Сохранение урока в проект {p}", messages=[_user_msg(msg)])

    if name == "weekly-review":
        scope = f"проекту «{project}»" if project else "всем проектам"
        proj_arg = f"project={project}" if project else "project=all"
        msg = (f"Сделай еженедельный обзор по {scope}: собери из базы знаний memory-compiler последние решения "
               f"(search_decisions), изменения статусов (get_current для release/deployment/config), открытые "
               f"вопросы из последних сессий и knowledge gaps (gap_report, {proj_arg}). Сведи в краткий отчёт: "
               f"что сделано, что в работе, что требует внимания.")
        return GetPromptResult(description=f"Еженедельный обзор ({scope})", messages=[_user_msg(msg)])

    raise ValueError(f"Неизвестный промпт: {name}")


# --- Completion (P2): автодополнение аргументов промптов/ресурсов ------------
# Клиент подсказывает валидные имена проектов (в слэш-командах и в шаблоне
# memory://{project}/{filename}) и имена статей по мере ввода. Секреты/служебные
# файлы в подсказки не попадают.
def _project_names() -> list[str]:
    kd = config.KNOWLEDGE_DIR
    if not kd or not kd.exists():
        return []
    return sorted(
        p.name for p in kd.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "daily"
    )


def _article_names(project: str) -> list[str]:
    kd = config.KNOWLEDGE_DIR
    if not kd or not project:
        return []
    pdir = kd / project
    if not pdir.is_dir():
        return []
    return sorted(a.name for a in pdir.glob("*.md") if not _is_meta_file(a.name))


def _filter_candidates(cands: list[str], value: str) -> list[str]:
    v = (value or "").strip().lower()
    if not v:
        return cands
    prefix = [c for c in cands if c.lower().startswith(v)]
    return prefix if prefix else [c for c in cands if v in c.lower()]


@app.completion()
async def complete(ref, argument, context=None) -> Completion:
    name = getattr(argument, "name", None)
    value = getattr(argument, "value", "") or ""
    if name == "project":
        vals = _filter_candidates(_project_names(), value)
        return Completion(values=vals[:100], total=len(vals), hasMore=len(vals) > 100)
    if name == "filename":
        proj = ""
        ctx_args = getattr(context, "arguments", None) if context else None
        if ctx_args:
            proj = (ctx_args or {}).get("project", "") or ""
        vals = _filter_candidates(_article_names(proj), value)
        return Completion(values=vals[:100], total=len(vals), hasMore=len(vals) > 100)
    return Completion(values=[], total=0, hasMore=False)


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    obs.new_request_id()          # корреляция всех логов этого вызова
    obs.record_call(name)
    _log = obs.get_logger("tool")
    t0 = time.perf_counter()
    # Count every tool call (not only predefined keys)
    stats[name] = stats.get(name, 0) + 1

    # Normalize project name in arguments — single source of truth.
    # Eliminates MyProj vs myproj splits regardless of how the caller spelled it.
    # 'all' is a special filter sentinel — preserve as-is.
    if "project" in arguments and isinstance(arguments["project"], str):
        from memory_compiler.storage import normalize_project
        proj = arguments["project"]
        if proj and proj.lower() != "all":
            arguments["project"] = normalize_project(proj)

    try:
        result = await _dispatch_tool(name, arguments)
    except ValueError as e:
        # safe_project_dir / safe_article_path raised — handler got an unsafe
        # project/filename parameter. Return graceful error instead of crashing.
        result = [TextContent(type="text", text=f"❌ Небезопасный параметр: {e}")]
    except Exception as e:
        # Раньше упавшие вызовы никак не фиксировались — статистика ошибок была слепой.
        code = type(e).__name__
        obs.record_error(name, code)
        _log.error(f"tool {name} failed: {e}", extra={"tool": name, "err_code": code}, exc_info=True)
        try:
            audit_log(name, arguments, 0, error=code)
        except Exception:
            pass
        raise
    # Track response size (result может содержать ResourceLink без .text)
    total = sum(len(getattr(t, "text", "") or "") for t in result)
    stats["total_chars_returned"] = stats.get("total_chars_returned", 0) + total
    audit_log(name, arguments, total)
    _log.info("tool ok", extra={"tool": name, "dur_ms": int((time.perf_counter() - t0) * 1000), "size": total})
    # У search объявлен outputSchema — обязаны вернуть structuredContent (SDK валидирует).
    # Строим из уже готовых resource_link-блоков content: программный клиент получает
    # машиночитаемый список, человекочитаемый текст + ссылки остаются в content.
    if name == "search":
        return (result, _build_search_structured(arguments.get("query", ""), result))
    return result


def _build_search_structured(query: str, blocks: list) -> dict:
    results = []
    for b in blocks:
        if getattr(b, "type", None) == "resource_link":
            results.append({
                "uri": str(b.uri),
                "name": b.name or "",
                "title": b.title or "",
                "score": b.description or "",
            })
    return {"query": query, "count": len(results), "results": results}


async def _dispatch_tool(name: str, arguments: dict) -> list[TextContent]:
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
        started = start_background_reindex()
        if started:
            result = [TextContent(type="text", text="\ud83d\udd04 Reindex \u0437\u0430\u043f\u0443\u0449\u0435\u043d \u0432 \u0444\u043e\u043d\u0435 \u2014 \u0441\u0435\u0440\u0432\u0435\u0440 \u043e\u0441\u0442\u0430\u0451\u0442\u0441\u044f \u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d. \u041d\u0430 \u0431\u043e\u043b\u044c\u0448\u043e\u0439 \u0431\u0430\u0437\u0435 (NAS) \u044d\u0442\u043e \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u043c\u0438\u043d\u0443\u0442; \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u0435 \u0432\u0438\u0434\u043d\u043e \u043f\u043e \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044e .embeddings.pkl.")]
        else:
            result = [TextContent(type="text", text="\u23f3 Reindex \u0443\u0436\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u2014 \u0434\u043e\u0436\u0434\u0438\u0441\u044c \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f.")]
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
    elif name == "init_schema":
        result = await handlers.init_schema(**arguments)
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
    elif name == "search_snippets":
        result = await handlers.search_snippets(**arguments)
    elif name == "save_runbook":
        result = await handlers.save_runbook(**arguments)
    elif name == "get_runbook":
        result = await handlers.get_runbook(**arguments)
    elif name == "search_error":
        result = await handlers.search_error(**arguments)
    elif name == "set_project_deps":
        result = await handlers.set_project_deps(**arguments)
    elif name == "get_project_deps":
        result = await handlers.get_project_deps(**arguments)
    elif name == "save_decision":
        result = await handlers.save_decision(**arguments)
    elif name == "search_decisions":
        result = await handlers.search_decisions(**arguments)
    elif name == "save_from_template":
        result = await handlers.save_from_template(**arguments)
    elif name == "list_templates":
        result = await handlers.list_templates()
    elif name == "save_secret":
        result = await handlers.save_secret(**arguments)
    elif name == "git_capture":
        result = await handlers.git_capture(**arguments)
    elif name == "ingest":
        result = await handlers.ingest(**arguments)
    elif name == "import_obsidian":
        result = await handlers.import_obsidian(**arguments)
    elif name == "knowledge_gap":
        result = await handlers.knowledge_gap(**arguments)
    elif name == "save_tracking":
        result = await handlers.save_tracking(**arguments)
    elif name == "get_current":
        result = await handlers.get_current(**arguments)
    elif name == "route_project":
        result = await handlers.route_project(**arguments)
    elif name == "gap_report":
        result = await handlers.gap_report(**arguments)
    elif name == "stale_facts":
        result = await handlers.stale_facts(**arguments)
    elif name == "save_compact":
        result = await handlers.save_compact(**arguments)
    elif name == "consolidate":
        result = await handlers.consolidate(**arguments)
    else:
        result = [TextContent(type="text", text=f"\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 \u0438\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d\u0442: {name}")]
    return result
