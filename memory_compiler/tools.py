"""MCP tool definitions and dispatch."""
import json
import re
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
from memory_compiler.storage import regenerate_index, audit_log, _parse_frontmatter
from memory_compiler import handlers
from memory_compiler import obs
from memory_compiler import freshness
from memory_compiler import i18n
from memory_compiler.i18n import localize_tools, localize_prompts

app = Server("memory-compiler")


# --- Маркер обязательности (v1.54.0) ----------------------------------------
# Клиент срезает `required` у СТРОКОВЫХ параметров (замер и разбор — в docstring
# tests/test_tool_schemas.py), поэтому единственный доезжающий до модели канал —
# description. Наличия описания оказалось мало: «Почему выбрали это» объясняет
# смысл поля и молчит про обязательность, и на этом пять раз подряд упал
# save_decision (2026-08-02, в аудите прода записей нет вовсе — вызовы отбивал
# клиент). Маркер дописывается ЗДЕСЬ, а не руками у 60 параметров: источник
# правды один — `required` схемы, и на новом инструменте забыть нельзя.
_REQUIRED_MARK_RU = " (обязательно)"
_REQUIRED_MARK_EN = " (required)"


def _mark_required(tools: list[Tool]) -> list[Tool]:
    """Дописать маркер обязательности в description обязательных строк.

    Язык читается в момент вызова через модуль i18n, а не берётся импортом
    значения: тесты переключают MC_LANG через monkeypatch на модуле, и снимок
    константы остался бы русским. Правка идёт по месту — list_tools() собирает
    объекты заново на каждый вызов, чужие Tool сюда не попадают.
    """
    mark = _REQUIRED_MARK_EN if i18n.MC_LANG == "en" else _REQUIRED_MARK_RU
    for tool in tools:
        schema = tool.inputSchema or {}
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            spec = props.get(name)
            if not isinstance(spec, dict) or spec.get("type") != "string":
                continue
            text = (spec.get("description") or "").rstrip()
            # Проверяем ОБА маркера: иначе смена языка наслоила бы второй.
            if text.endswith((_REQUIRED_MARK_RU, _REQUIRED_MARK_EN)):
                continue
            spec["description"] = f"{text}{mark}".lstrip()
    return tools


# --- Tool annotations (MCP hints для клиента, напр. Claude Desktop) ---------
# Классификация статична (per tool). Принцип: «может мутировать» => readOnlyHint=False,
# даже если дефолтные аргументы читают (lint fix=False, compile dry_run=True) — иначе
# клиент авто-подтвердит потенциально пишущий вызов.
_READONLY_LOCAL = frozenset({
    "get_context", "search", "load_session", "get_summary", "ask",
    "get_active_context", "read_article", "search_by_tag", "article_history", "backlinks",
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
                    "force_new": {"type": "boolean", "default": False, "description": "Принудительно создать новую статью"},
                    "supersedes": {"type": "string", "description": "Имена файлов статей, которые эта поправка ОТМЕНЯЕТ (через запятую). Ставить всегда, когда выяснилось, что прежний вывод неверен: без этого обе статьи выдаются равноправно и следующая сессия возьмёт ту, что выше по релевантности, а не ту, что верна"}
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
                    "query": {"type": "string", "description": "Поисковый запрос"},
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
                                # Описания здесь ПО-АНГЛИЙСКИ: i18n.localize_tools переводит
                                # только description инструмента и inputSchema, outputSchema
                                # он не трогает — кириллица тут непереводима в принципе и
                                # роняет гейт «при MC_LANG=en не осталось кириллицы».
                                "uri": {"type": "string", "description": "memory://<project>/<file>; for a secret article it does NOT resolve as a resource — read it via read_article"},
                                "name": {"type": "string"},
                                "title": {"type": "string"},
                                "score": {"type": "string"},
                                "project": {"type": "string", "description": "argument for read_article"},
                                "file": {"type": "string", "description": "argument for read_article"},
                                "secret": {"type": "boolean", "description": "body is encrypted; opens only via read_article"}
                            },
                            "required": ["uri", "name"]
                        }
                    },
                    # Футеры (свежесть, подсказка при первом обращении к проекту)
                    # ДУБЛИРУЮТСЯ сюда. У search объявлен outputSchema, и клиент
                    # берёт structuredContent — дополнительный TextContent до
                    # модели не доходит. Проверено на проде: сервер отдавал
                    # подсказку вторым текстовым блоком, а она никуда не ехала.
                    "notice": {"type": "string", "description": "server-side note: freshness warning or project context hint"}
                },
                "required": ["query", "count", "results"]
            },
            # MCP Apps: ссылка на вьюху. Ключ передаётся ПО АЛИАСУ `_meta` — у Tool
            # не выставлен populate_by_name, поэтому Tool(meta=...) не заполняет
            # НИЧЕГО и молча: исключения нет, поле остаётся None, а хост потом
            # просто не находит ссылку. Держит tests/test_mcp_apps.py.
            **{"_meta": {"ui": {"resourceUri": UI_SEARCH_RESOURCE}}}
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
                    "fix": {"type": "boolean", "default": False, "description": "Автоисправление безопасных проблем (теги, index)"},
                    "verbose": {"type": "boolean", "default": False, "description": "Развернуть построчно то, что по умолчанию свёрнуто в счётчик (устаревшие статьи, сироты)"}
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
            name="open_questions",
            description=(
                "Показать НЕЗАКРЫТЫЕ вопросы проекта — на чём останавливались в прошлых "
                "сессиях и что осталось нерешённым. Вызывать при возврате к проекту."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта или 'all' (по умолчанию all)"}
                },
            }
        ),
        Tool(
            name="session_note",
            description=(
                "Записать заметку ПО ХОДУ работы — одной строкой, не дожидаясь конца "
                "сессии: что выяснилось, что проверено, где затык. Дёшево: сводка "
                "сессии не пересобирается. Вызывать сразу, как появился факт, который "
                "пригодится параллельной сессии или следующему старту."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "Что выяснилось — одна-две фразы (обязательно)"},
                    "project": {"type": "string", "description": "Имя проекта (обязательно)"}
                },
                "required": ["note", "project"]
            }
        ),
        Tool(
            name="close_question",
            description=(
                "Закрыть решённый открытый вопрос проекта. Ищет по куску текста вопроса."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта (обязательно)"},
                    "match": {"type": "string", "description": "Кусок текста вопроса, который закрываем (обязательно)"},
                    "remainder": {"type": "string", "description": "Живой ОСТАТОК вопроса, если решена лишь часть: он заведётся отдельным открытым вопросом. Половина вопросов склеена из нескольких тем — без остатка закрытие хоронит нерешённые пункты"}
                },
                "required": ["project", "match"]
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
                    "open_questions": {"type": "string", "description": "Что осталось НЕЯСНЫМ — конкретный нерешённый вопрос. Не список запланированных работ: перечень задач живёт в итоге сессии, а сюда идёт то, на что нужен ответ"}
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
            name="context_gaps",
            description="Выдать статьи, которым нужен ИИ-контекст секций (для генерации). "
                        "Многосекционные не-секретные без contexts. Затем — save_contexts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "all", "description": "Проект или 'all'"},
                    "limit": {"type": "integer", "default": 5, "description": "Сколько статей за раз"}
                }
            }
        ),
        Tool(
            name="save_contexts",
            description="Сохранить ИИ-контексты секций во frontmatter статьи и ре-эмбеддить.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта"},
                    "filename": {"type": "string", "description": "Имя файла статьи"},
                    "contexts": {
                        "type": "array",
                        "description": "Список {heading, context} по секциям",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heading": {"type": "string"},
                                "context": {"type": "string"}
                            },
                            "required": ["heading", "context"]
                        }
                    }
                },
                "required": ["project", "filename", "contexts"]
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
            name="backlinks",
            description=("Кто ссылается на статью: обратные РУЧНЫЕ связи "
                         "([[вики-ссылки]] и markdown-ссылки в теле) со строкой "
                         "контекста. Авто-блок «См. также» не учитывается — он про "
                         "семантическую близость, её показывает related."),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта статьи"},
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
                    "open_questions": {"type": "string", "description": "Что осталось НЕЯСНЫМ — конкретный нерешённый вопрос. Не список запланированных работ: перечень задач живёт в итоге сессии, а сюда идёт то, на что нужен ответ"}
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
                    "project": {"type": "string", "description": "Имя проекта"},
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
                    "project": {"type": "string", "description": "Имя проекта"},
                    "filename": {"type": "string", "description": "Имя файла runbook"}
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
                    "project": {"type": "string", "description": "Имя проекта"},
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
                    "project": {"type": "string", "description": "Имя проекта"}
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
                    "alternatives": {"type": "string", "description": "Какие были альтернативы (необязательно; пусто = не рассматривались)"},
                    "reasoning": {"type": "string", "description": "Почему выбрали это"},
                    "project": {"type": "string", "description": "Имя проекта"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["title", "decision", "reasoning", "project"]
            }
        ),
        Tool(
            name="search_decisions",
            description="Поиск по журналу решений.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
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
                    "project": {"type": "string", "description": "Имя проекта"},
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
                    "project": {"type": "string", "description": "Имя проекта"},
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
                    "project": {"type": "string", "description": "Имя проекта"},
                    "entity": {"type": "string", "description": "Название сущности: release, deployment, config"}
                },
                "required": ["project", "entity"]
            }
        ),
        Tool(
            name="consolidate",
            description="Найти дубли/похожие статьи: near-exact детектор РЕАЛЬНЫХ дублей (точный/containment матч по тексту) + похожие темы по embeddings. НЕ мержит автоматически.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "all"},
                    "min_sim": {"type": "number", "default": 0.985, "description": "Порог embedding-similarity для «похожих тем» (e5). 0.985 — почти дубли; ниже — на коротком RU-корпусе много ложняков. Реальные дубли ловит near-exact, не порог."}
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
                    "project": {"type": "string", "description": "Имя проекта"},
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
                    "project": {"type": "string", "description": "Имя проекта"},
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
    # Маркер ПОСЛЕ локализации: иначе он лёг бы на русский текст и был бы затёрт
    # английским переводом описания.
    return _mark_required(localize_tools(tools))


# --- Resources (P1): статьи базы как memory://<проект>/<файл> ----------------
# База становится first-class контекстом: клиент (Claude Desktop) листает и
# @-упоминает статьи без tool-вызова. Секреты не отдаются: secret_*.md и статьи
# с SECRET_FLAG исключаются из листинга; read_resource редактирует инлайн ENC:.
_RESOURCE_MIME = "text/markdown"
_RESOURCE_SCHEME = "memory://"

# --- MCP Apps (P3): вьюха результатов поиска как ui://-ресурс ----------------
# Расширение io.modelcontextprotocol/ui, спека 2026-01-26. Хост берёт HTML по
# ссылке из `_meta.ui.resourceUri` инструмента и рисует его в песочном iframe.
# MIME — РОВНО тот, что клиент объявляет на initialize (зонд v1.51.2 показал
# text/html;profile=mcp-app). Другой MIME = хост не возьмёт ресурс, панель не
# отрисуется, и выглядеть это будет как «клиент не умеет MCP Apps».
# В resources/list ui:// НЕ показываем — спека разрешает, а листинг у нас про
# статьи базы. Держит tests/test_mcp_apps.py.
UI_SCHEME = "ui://"
UI_MIME = "text/html;profile=mcp-app"
UI_SEARCH_RESOURCE = "ui://memory-compiler/search-results.html"


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
    # От ТЕЛА: '---' не вызывал break, а следующая строка 'contexts:' вызывала —
    # до '# Заголовка' цикл не доходил, и у 125 статей заголовком ресурса
    # становилось имя файла.
    text = _parse_frontmatter(text)[1]
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:120]
        if s and not s.startswith("---"):
            break
    return filename[:-3] if filename.endswith(".md") else filename


def _resource_description(text: str) -> str:
    # От ТЕЛА: иначе первой подходящей строкой оказывался литерал 'contexts:' —
    # именно он и уезжал в описание 125 MCP-ресурсов, то есть в пассивный
    # контекст модели.
    text = _parse_frontmatter(text)[1]
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
    if uri_s.startswith(UI_SCHEME):
        # Вьюха MCP Apps. Отдаётся до всякой работы с базой: это статика, ни
        # проекта, ни файла тут нет, и путь в knowledge/ по ui:// не строится.
        from memory_compiler.ui_app import SEARCH_VIEW_HTML
        if uri_s == UI_SEARCH_RESOURCE:
            return [ReadResourceContents(content=SEARCH_VIEW_HTML, mime_type=UI_MIME)]
        return notice(f"❌ Неизвестный ui-ресурс: {uri_s}")
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
    return localize_prompts(_PROMPTS)


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


# ─── Guard от утёкшей разметки вызова ────────────────────────────────────────
# Клиентский парсер иногда не видит границу параметра (модель пишет закрывающие
# теги без обязательного префикса) и доедает остаток блока вызова в строковое
# значение: «…текст.</content>\n<session_summary>…</session_summary>\n</invoke>».
# Замер 2026-07-27: 208 живых статей (~11% базы) с таким хвостом, у ~50 сессий
# session_summary/open_questions потеряны, tags ставил только авто-теггер.
# Лечение на транспортной границе: хвост отрезается, поля доезжают по назначению.
# Якорь — ТОЛЬКО на конце строки: упоминания тегов в середине текста (статьи про
# сам баг) не трогаются. Словарь тегов — имена параметров самого тула из схемы.

_INVOKE_CLOSE = "</invoke>"
_PARAM_CLOSE = "</parameter>"
_TOOL_PROPS: dict | None = None


async def _tool_props() -> dict:
    """Ленивая карта {tool: properties} из объявленных схем — словарь имён
    параметров для heal_arguments. Схемы статичны, строится один раз."""
    global _TOOL_PROPS
    if _TOOL_PROPS is None:
        _TOOL_PROPS = {tl.name: (tl.inputSchema or {}).get("properties", {}) or {}
                       for tl in await list_tools()}
    return _TOOL_PROPS


def _trailing_field(work: str, others: set) -> tuple | None:
    """Замыкающий блок чужого поля: '<q>…</q>' или '<parameter name="q">…</parameter>'
    (q — параметр того же тула). None, если конец строки — не такой блок."""
    for q in sorted(others, key=len, reverse=True):
        close = f"</{q}>"
        if work.endswith(close):
            i = work.rfind(f"<{q}>")
            if i >= 0:
                raw = work[i + len(q) + 2:-len(close)]
                return q, raw.strip(), work[:i].rstrip()
    if work.endswith(_PARAM_CLOSE):
        i = work.rfind('<parameter name="')
        if i >= 0:
            rest = work[i + len('<parameter name="'):]
            q, sep, tail = rest.partition('">')
            if sep and q in others:
                return q, tail[:-len(_PARAM_CLOSE)].strip(), work[:i].rstrip()
    return None


_PARAM_OPEN_RE = re.compile(r'^<parameter name="([A-Za-z_][A-Za-z0-9_]*)">(.*)$', re.S)


def _trailing_open_field(work: str, others: set) -> tuple | None:
    """Замыкающая СТРОКА вида '<parameter name="q">…' — форма, где клиент не дописал
    закрывающий тег вовсе (v1.54.3).

    Отличается от _trailing_field тем, что блок НЕ ЗАКРЫТ: content закрыт нормально,
    а следом с новой строки въехали поля. Замер 2026-08-12: 216 живых статей вне
    daily/, свежайшая — того же дня; потеряно tags 177, session_summary 69,
    open_questions 38.

    Якорь строгий — блок обязан начинаться С НАЧАЛА СТРОКИ. Ровно это отличает хвост
    вызова от прозы про него: в статьях о самом баге разметка стоит внутри фразы.
    """
    head, sep, last = work.rpartition("\n")
    if not sep:
        return None
    m = _PARAM_OPEN_RE.match(last)
    if not m:
        return None
    q, raw = m.group(1), m.group(2)
    if q not in others:
        return None
    for close in (f"</{q}>", _PARAM_CLOSE):
        if raw.endswith(close):
            raw = raw[:-len(close)]
            break
    return q, raw.strip(), head.rstrip()


def _parse_leaked(raw: str, prop: dict):
    """Значение утёкшего поля по типу из схемы. Непарсибельное — None (поле
    не восстанавливаем: содержимое уже мусор, а падать нельзя)."""
    raw = raw.strip()
    ptype = prop.get("type")
    if ptype in (None, "string"):
        return raw or None
    try:
        val = json.loads(raw)
    except Exception:
        return None
    if ptype == "array" and not isinstance(val, list):
        return None
    return val


def heal_arguments(arguments: dict, props: dict) -> tuple[dict, list]:
    """Отрезать утёкший хвост разметки у строковых параметров и вернуть
    восстановленные поля по назначению (явно переданные не перекрываются).
    Возвращает (аргументы, список вылеченного) — список пуст у здоровых вызовов."""
    healed: list = []
    out = dict(arguments)
    for key, val in arguments.items():
        if not isinstance(val, str) or key not in props:
            continue
        text = val.rstrip()
        touched = False
        if text.endswith(_INVOKE_CLOSE):
            text = text[:-len(_INVOKE_CLOSE)].rstrip()
            touched = True
        fields: dict = {}
        work = text
        open_tail = False
        while True:
            hit = _trailing_field(work, set(props) - {key})
            if not hit:
                hit = _trailing_open_field(work, set(props) - {key})
                if hit:
                    open_tail = True
            if not hit:
                break
            q, raw, work = hit
            fields.setdefault(q, raw)
        anchor = next((a for a in (f"</{key}>", _PARAM_CLOSE) if work.endswith(a)), None)
        if anchor:
            out[key] = work[:-len(anchor)].rstrip()
        elif open_tail and work.count("```") % 2 == 0:
            # Хвост БЕЗ якоря: параметр закрыт корректно, а следом с новой строки
            # въехали чужие поля. Чётность ``` — защита от статьи, которая ПОКАЗЫВАЕТ
            # эту форму внутри блока кода: отрезав хвост, guard разорвал бы блок и
            # съел содержательный пример.
            out[key] = work.rstrip()
        elif touched:
            out[key] = text        # без якоря блоки не трогаем — только срез </invoke>
            fields = {}
        else:
            continue
        healed.append(key)
        for q, raw in fields.items():
            if out.get(q) in (None, "", []):
                parsed = _parse_leaked(raw, props.get(q, {}))
                if parsed is not None:
                    out[q] = parsed
                    healed.append(f"+{q}")
    return out, healed


# --- Зонд MCP Apps: что клиент объявляет на initialize -----------------------
# Расширение io.modelcontextprotocol/ui (спека 2026-01-26) хост объявляет САМ:
# capabilities.extensions["io.modelcontextprotocol/ui"] = {"mimeTypes": [...]}.
# Это прямой машиночитаемый ответ на «умеет ли клиент MCP Apps» — надёжнее, чем
# смотреть глазами, отрисовалась ли панель. Опора: 1.28.1 поле extensions не
# моделирует, но ClientCapabilities.model_config extra="allow" и оно переживает
# валидацию. Смена этого поведения на бампе SDK ослепит зонд МОЛЧА — держит
# tests/test_client_capabilities.py.
UI_EXTENSION = "io.modelcontextprotocol/ui"

_seen_clients: set[str] = set()


def client_ui_support(params) -> dict:
    """Что клиент объявил про UI-расширение. Чистая функция — тестируется без сессии."""
    if params is None:
        return {"client": "?", "version": "?", "ui_extension": None, "extensions": []}
    info = getattr(params, "clientInfo", None)
    caps = getattr(params, "capabilities", None)
    ext = {}
    if caps is not None:
        ext = caps.model_dump(by_alias=True, exclude_none=True).get("extensions") or {}
    return {
        "client": getattr(info, "name", None) or "?",
        "version": getattr(info, "version", None) or "?",
        "ui_extension": ext.get(UI_EXTENSION),
        "extensions": sorted(ext),
    }


def _log_client_once() -> None:
    """Одна запись на уникального клиента: поддержка UI — свойство клиента, а не
    вызова, и капать в лог на каждый tool-call ей незачем."""
    try:
        params = app.request_context.session.client_params
    except Exception:
        return                    # вне запроса — молча, зонд не смеет ронять вызов
    info = client_ui_support(params)
    key = f"{info['client']}/{info['version']}"
    if key in _seen_clients:
        return
    _seen_clients.add(key)
    obs.get_logger("client").info("client connected", extra=info)


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    obs.new_request_id()          # корреляция всех логов этого вызова
    obs.record_call(name)
    _log_client_once()
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

    healed: list = []
    props = (await _tool_props()).get(name)
    if props:
        arguments, healed = heal_arguments(arguments, props)
    audit_args = {**arguments, "_healed": healed} if healed else arguments
    if healed:
        _log.info("leaked markup healed", extra={"tool": name, "healed": ",".join(healed)})

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
            audit_log(name, audit_args, 0, error=code)
        except Exception:
            pass
        raise
    # Свежесть контекста между сессиями: сервер знает про все записи, поэтому
    # может сам сказать этой сессии, что под ней изменилось. Считаем ДО audit_log
    # и до подсчёта размера — футер тоже часть ответа.
    result = _append_freshness(name, arguments, result)

    # Track response size (result может содержать ResourceLink без .text)
    total = sum(len(getattr(t, "text", "") or "") for t in result)
    stats["total_chars_returned"] = stats.get("total_chars_returned", 0) + total
    audit_log(name, audit_args, total)
    _log.info("tool ok", extra={"tool": name, "dur_ms": int((time.perf_counter() - t0) * 1000), "size": total})
    # У search объявлен outputSchema — обязаны вернуть structuredContent (SDK валидирует).
    # Строим из уже готовых resource_link-блоков content: программный клиент получает
    # машиночитаемый список, человекочитаемый текст + ссылки остаются в content.
    if name == "search":
        payload = _build_search_structured(arguments.get("query", ""), result)
        # футер, добавленный _append_freshness последним TextContent, дублируем
        # в структурированную выдачу — иначе он не доедет до модели (см.
        # _merge_notice_into_payload)
        notice = result[-1].text if result and getattr(result[-1], "type", "") == "text"             and str(getattr(result[-1], "text", "")).lstrip().startswith(("📌", "⚠️", "💡")) else ""
        return (result, _merge_notice_into_payload(payload, notice))
    return result



def _merge_notice_into_payload(payload: dict, notice: str) -> dict:
    """Продублировать футер в структурированную выдачу.

    Клиент с поддержкой outputSchema читает structuredContent и дополнительный
    TextContent модели не показывает — без этого подсказка не доезжает именно
    там, где она нужнее всего: `search` чаще всего и открывает «слепую» сессию.
    """
    if not notice or not isinstance(payload, dict):
        return payload
    out = dict(payload)
    out["notice"] = notice.strip()
    return out


# Инструменты, чья запись делает контекст ДРУГИХ сессий устаревшим.
_FRESHNESS_WRITE_TOOLS = {
    "save_lesson", "save_decision", "save_runbook", "save_secret", "save_tracking",
    "save_session", "save_from_template", "save_contexts", "save_compact",
    "finish_task", "edit_article", "delete_article", "consolidate", "ingest",
    "session_note",
}


# Инструменты, которые САМИ отдают контекст проекта: подсказка при первом
# обращении дублировала бы их выдачу.
_CONTEXT_TOOLS = {"start_task", "load_session", "get_active_context",
                  "open_questions", "get_context", "get_summary"}


def _append_freshness(name: str, arguments: dict, result: list) -> list:
    """Дописать к ответу предупреждение о чужих записях в этом проекте.

    ⚠️ Отдельным блоком, а не приклейкой к существующему тексту: у search есть
    outputSchema и resource_link-блоки, а 414 ассертов в тестах сравнивают тексты
    ответов дословно. Отдельный TextContent появляется ТОЛЬКО когда есть что
    сказать, поэтому обычные ответы остаются байт-в-байт прежними.
    """
    try:
        session = app.request_context.session
    except Exception:
        return result                     # вне запроса (REST, тесты) — не мешаем
    key = freshness.key_for(session)
    project = arguments.get("project") if isinstance(arguments, dict) else None
    # ⚠️ Спрашиваем ДО consume: тот делает touch и признак первого касания стирает.
    first = (freshness.is_first_touch(key, project or "")
             and name not in _CONTEXT_TOOLS)
    try:
        note = freshness.consume(key, project or "")
        if first:
            note = handlers.first_touch_context(project) + note
        if name in _FRESHNESS_WRITE_TOOLS and project and project != "all":
            topic = ""
            if isinstance(arguments, dict):
                # у session_note нет ни topic, ни filename — иначе чужая сессия
                # увидит «session_note: (без темы)» и не поймёт, что изменилось
                topic = str(arguments.get("topic") or arguments.get("filename")
                            or arguments.get("note") or "")
            freshness.note_write(project, name, topic, key)
    except Exception:
        return result                     # сторож не имеет права ронять вызов
    if note:
        return list(result) + [TextContent(type="text", text=note)]
    return result


def _build_search_structured(query: str, blocks: list) -> dict:
    """Структурированная выдача search.

    Основной источник — payload, собранный самим хендлером: он один знает про
    секретность. Сборка из resource-ссылок (ниже, как фолбэк) секреты ТЕРЯЛА —
    ссылок на них нет намеренно, и панель MCP Apps молча показывала меньше
    результатов, чем текстовая выдача того же вызова, включая счётчик.
    """
    payload = handlers.search_payload_var.get()
    if payload is not None and payload.get("query") == query:
        return payload
    results = []
    for b in blocks:
        if getattr(b, "type", None) == "resource_link":
            results.append({
                "uri": str(b.uri),
                "name": b.name or "",
                "title": b.title or "",
                "score": b.description or "",
                "project": (b.name or "/").split("/", 1)[0],
                "file": (b.name or "/").split("/", 1)[-1],
                "secret": False,
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
        result = await handlers.lint(arguments.get("project", "all"), arguments.get("fix", False),
                                     arguments.get("verbose", False))
    elif name == "reindex":
        started = start_background_reindex()
        if started:
            result = [TextContent(type="text", text="\U0001F504 Reindex \u0437\u0430\u043f\u0443\u0449\u0435\u043d \u0432 \u0444\u043e\u043d\u0435 \u2014 \u0441\u0435\u0440\u0432\u0435\u0440 \u043e\u0441\u0442\u0430\u0451\u0442\u0441\u044f \u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d. \u041d\u0430 \u0431\u043e\u043b\u044c\u0448\u043e\u0439 \u0431\u0430\u0437\u0435 (NAS) \u044d\u0442\u043e \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u043c\u0438\u043d\u0443\u0442; \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u0435 \u0432\u0438\u0434\u043d\u043e \u043f\u043e \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044e .embeddings.pkl.")]
        else:
            result = [TextContent(type="text", text="\u23f3 Reindex \u0443\u0436\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u2014 \u0434\u043e\u0436\u0434\u0438\u0441\u044c \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f.")]
    elif name == "open_questions":
        result = await handlers.open_questions(arguments.get("project", "all"))
    elif name == "session_note":
        result = await handlers.session_note(**arguments)
    elif name == "close_question":
        result = await handlers.close_question(**arguments)
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
    elif name == "context_gaps":
        result = await handlers.context_gaps(**arguments)
    elif name == "save_contexts":
        result = await handlers.save_contexts(**arguments)
    elif name == "read_article":
        result = await handlers.read_article(**arguments)
    elif name == "search_by_tag":
        result = await handlers.search_by_tag(**arguments)
    elif name == "article_history":
        result = await handlers.article_history(**arguments)
    elif name == "backlinks":
        result = await handlers.backlinks(**arguments)
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
