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
from memory_compiler import storage
from memory_compiler import handlers
from memory_compiler import obs
from memory_compiler import freshness
from memory_compiler import i18n
from memory_compiler.i18n import localize_tools, localize_prompts

# version обязателен: без него SDK кладёт в serverInfo СВОЮ версию (pkg_version("mcp")),
# и клиент видит «memory-compiler 1.29.1» — так сервер назвал багрепорт 22.09.2026.
app = Server("memory-compiler", version=config.VERSION)


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


# --- Служебный параметр _client_session (v1.77.0) ---------------------------
# Мост Claude Desktop пропускает к серверу только аргументы из схемы: правка
# объявленного query у search от PreToolUse-хука доехала, а необъявленный
# _client_session тот же путь выбросил (проба 2026-09-11). Объявляется ЗДЕСЬ, у
# всех инструментов разом, — как и маркер, чтобы на новом инструменте не забыть.
# Сервер вынимает аргумент в call_tool до аудита и хендлера.
_CLIENT_SESSION_DESC_RU = "Служебное: id чата для свежести контекста, ставит хук клиента. Не заполнять."
_CLIENT_SESSION_DESC_EN = "Service field: chat id for context freshness, set by the client hook. Do not fill."


# Инструменты, которым проект можно взять из истории сессии, если вызов его не
# назвал. У них `project` снимается с `required`: пока он там стоит, клиентский
# валидатор отбивает вызов РАНЬШЕ сервера («expected string, received undefined»,
# живая проверка 20.09.2026 на проде), и серверная подстановка не достижима.
# ⚠️ Деструктивных здесь нет и быть не должно: цена ошибки — стёртая статья.
# ⚠️ Поиска здесь нет: там `project` и так необязателен, 'all' — рабочий режим.
_PROJECT_FROM_SESSION = frozenset({
    "finish_task", "save_lesson", "save_decision", "save_runbook", "save_session",
    "save_secret", "save_tracking", "save_contexts", "save_compact",
    "save_from_template", "session_note", "edit_article", "close_question",
})


# Инструменты, которым позволено ЗАВЕСТИ проект: проект в базе появляется первой
# записью знания в него. Всем остальным, кто принимает `project`, нужен уже
# существующий — несуществующий получает подсказку вместо вызова (_project_gate).
# ⚠️ Список держит РАЗРЕШЁННОЕ, а не запрещённое: новый инструмент, забытый здесь,
# на новом проекте ответит понятной подсказкой, а не заведёт каталог-пустышку.
_PROJECT_CREATORS = frozenset({
    "finish_task", "save_lesson", "save_decision", "save_runbook", "save_session",
    "save_secret", "save_tracking", "save_compact", "save_from_template",
    "session_note", "init_schema", "ingest", "import_obsidian", "git_capture",
})


def _relax_project_requirement(tools: list[Tool]) -> list[Tool]:
    """Снять `project` с `required` там, где сервер умеет подставить его сам.

    Параметр остаётся объявленным и описанным: явный проект — главный способ
    вызова, подстановка — страховка от потери записи. Шаг идёт ДО _mark_required,
    чтобы маркер « (обязательно)» не обещал того, чего схема уже не требует.
    """
    for tool in tools:
        if tool.name not in _PROJECT_FROM_SESSION:
            continue
        schema = tool.inputSchema or {}
        required = schema.get("required")
        if required and "project" in required:
            schema["required"] = [name for name in required if name != "project"]
    return tools


def _declare_client_session(tools: list[Tool]) -> list[Tool]:
    """Объявить необязательный строковый _client_session у каждого инструмента.

    Идёт последним шагом list_tools: ни перевод, ни маркер обязательности
    служебное описание не трогают. Язык читается так же, как в _mark_required.
    Без pattern и maxLength: кривое значение должно откатываться на ключ
    MCP-сессии в freshness.key_for, а не валить вызов на валидации схемы.
    """
    desc = _CLIENT_SESSION_DESC_EN if i18n.MC_LANG == "en" else _CLIENT_SESSION_DESC_RU
    for tool in tools:
        props = tool.inputSchema.setdefault("properties", {})
        props[freshness.CLIENT_SESSION_ARG] = {"type": "string", "description": desc}
    return tools


# --- Рефлексы памяти (v1.78.0) ------------------------------------------------
# Описание — единственный канал до модели (required у строк клиент срезает, см.
# test_tool_schemas): что это, в каком виде и чего туда НЕ класть.
_TRIGGERS_DESC = (
    "Когда статья должна всплыть САМА, без поиска: список строк «ошибка: <дословная строка "
    "ошибки>», «цель: <хост, IP, домен, контейнер>», «файл: <путь файла>». Хук клиента "
    "покажет статью, когда агент получит такую ошибку, пойдёт на эту цель или прочитает этот "
    "файл. Ошибку — дословно, не пересказом; пароли сюда не класть")

_VERIFY_DESC = (
    "Чем подтвердить факт об узле живой командой: список строк «<read-only команда> => "
    "<ожидаемое значение>», например «/system identity print => KHV-GW». Команда обязана "
    "только читать состояние. Статье нужен триггер «цель: <адрес>». Пароли сюда не класть")


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
                    "force_new": {"type": "boolean", "description": "Принудительно создать новую статью (по умолчанию false)"},
                    "verified": {"type": "string", "description": "ЧЕМ проверен факт: прогон тестов, живой вызов на проде, вывод команды, ответ API. Ставить, когда вывод получен инструментом, а не выведен косвенно — иначе следующая сессия примет догадку за проверенное"},
                    "supersedes": {"type": "string", "description": "Имена файлов статей, которые эта поправка ОТМЕНЯЕТ (через запятую). Ставить всегда, когда выяснилось, что прежний вывод неверен: без этого обе статьи выдаются равноправно и следующая сессия возьмёт ту, что выше по релевантности, а не ту, что верна"},
                    "triggers": {"type": "array", "items": {"type": "string"}, "description": _TRIGGERS_DESC},
                    "verify": {"type": "array", "items": {"type": "string"}, "description": _VERIFY_DESC}
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
                    "project": {"type": "string", "description": "Имя проекта или 'all'"}
                },
                "required": ["query"]
            },
            # Машиночитаемая выдача (structuredContent) для программных клиентов.
            # С v1.87.0 structuredContent и единственный текстовый блок несут ОДИН
            # и тот же JSON (handlers.search_json) — без resource_link. Схема
            # нестрогая (additionalProperties по умолчанию) — на этом держится
            # безопасный откат на релиз с uri/name, держит
            # test_release2_schema_accepts_release1_payload_for_rollback.
            outputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer"},
                    "fallback_from": {"type": "string", "description": "nothing was found in this project, so results come from all projects"},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                # Описания здесь ПО-АНГЛИЙСКИ: i18n.localize_tools переводит
                                # только description инструмента и inputSchema, outputSchema
                                # он не трогает — кириллица тут непереводима в принципе и
                                # роняет гейт «при MC_LANG=en не осталось кириллицы».
                                "title": {"type": "string"},
                                "project": {"type": "string", "description": "argument for read_article"},
                                "file": {"type": "string", "description": "argument for read_article"},
                                "score": {"type": "string"},
                                "secret": {"type": "boolean", "description": "body is encrypted; opens only via read_article"},
                                "superseded_by": {"type": "string", "description": "this article is superseded: read this file of the same project instead"},
                                "correction": {"type": "boolean", "description": "this article is a correction that supersedes an earlier one"},
                            },
                            "required": ["title", "project", "file"]
                        }
                    },
                    # Футеры (свежесть, подсказка при первом обращении к проекту)
                    # уходят СЮДА, а не отдельным текстовым блоком: с v1.87.0 у
                    # search он один и несёт тот же JSON. Причина та же, что и в
                    # v1.68.0: у search объявлен outputSchema, и клиент берёт
                    # structuredContent — дополнительный TextContent до модели не
                    # доходит. Проверено на проде: подсказка ехала вторым текстовым
                    # блоком и никуда не доезжала.
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
                    "dry_run": {"type": "boolean", "description": "Превью без изменений (по умолчанию true)"},
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
                    "project": {"type": "string", "description": "Имя проекта или 'all'"},
                    "fix": {"type": "boolean", "description": "Автоисправление безопасных проблем (теги, index) (по умолчанию false)"},
                    "verbose": {"type": "boolean", "description": "Развернуть построчно то, что по умолчанию свёрнуто в счётчик (устаревшие статьи, сироты) (по умолчанию false)"}
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
                    "project": {"type": "string", "description": "Имя проекта или 'all'"}
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
                    "content": {"type": "string", "description": "Новое содержимое (полная замена тела статьи). Можно не передавать, если передан triggers"},
                    "append": {"type": "boolean", "description": "True — дописать в конец, False — заменить тело"},
                    "triggers": {"type": "array", "items": {"type": "string"}, "description": _TRIGGERS_DESC},
                    "verify": {"type": "array", "items": {"type": "string"}, "description": _VERIFY_DESC}
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="context_gaps",
            description="Выдать статьи, которым нужен ИИ-контекст секций (для генерации). "
                        "Многосекционные не-секретные без contexts. Затем — save_contexts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Проект или 'all'"},
                    "limit": {"type": "integer", "description": "Сколько статей за раз (по умолчанию 5)"}
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
            description=("Получить текст статьи. Служебные разделы «См. также», «Git-ссылки» и "
                         "frontmatter скрыты, в конце сноска о скрытом; full=true — статья целиком."),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Имя проекта или 'daily'"},
                    "filename": {"type": "string", "description": "Имя файла статьи"},
                    "full": {"type": "boolean",
                             "description": ("true — статья целиком, со служебными разделами и "
                                             "frontmatter. По умолчанию false")},
                },
                "required": ["project", "filename"]
            }
        ),
        Tool(
            name="search_by_tag",
            description="Найти статьи с указанным тегом: свежие сверху, по одной строке.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Тег для поиска"},
                    "project": {"type": "string", "description": "Имя проекта или 'all'"},
                    "limit": {"type": "integer",
                              "description": "Сколько статей показать. По умолчанию 30, максимум 200"},
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
                    "confirm": {"type": "boolean", "description": "Подтверждение удаления (обязательно если в проекте есть статьи) (по умолчанию false)"}
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
                    "open_questions": {"type": "string", "description": "Что осталось НЕЯСНЫМ — конкретный нерешённый вопрос. Не список запланированных работ: перечень задач живёт в итоге сессии, а сюда идёт то, на что нужен ответ"},
                    "triggers": {"type": "array", "items": {"type": "string"}, "description": _TRIGGERS_DESC}
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
                    "project": {"type": "string", "description": "Имя проекта или 'all' (по умолчанию all)"}
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
                    "project": {"type": "string", "description": "Имя проекта или 'all' (по умолчанию all)"}
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
                    "project": {"type": "string", "description": "Имя проекта или 'all' (по умолчанию all)"}
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
                    # ⚠️ Строка принимается наравне с объектом (v1.90.0). Живой случай
                    # 19.09.2026: суточная проверка узла ушла прозой, клиент отбил вызов
                    # целиком («expected object, received string») и запись пропала. Отказ
                    # клиентский, сервер такого вызова не видит — значит спасать нечем,
                    # кроме как разрешить тип. Строка ложится одним полем `note`.
                    "facts": {"type": ["object", "string"],
                              "description": "Факты: {version: '1.3.50', url: ...}. "
                                             "Можно строкой — она ляжет полем note"},
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
                    "project": {"type": "string", "description": "Имя проекта или 'all' (по умолчанию all)"},
                    "min_sim": {"type": "number", "description": "Порог embedding-similarity для «похожих тем» (e5). 0.985 — почти дубли; ниже — на коротком RU-корпусе много ложняков. Реальные дубли ловит near-exact, не порог."}
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
                    "project": {"type": "string", "description": "Имя проекта или 'all' (по умолчанию all)"},
                    "warn_days": {"type": "integer", "description": "За сколько дней предупреждать (по умолчанию 30)"}
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
                    "project": {"type": "string", "description": "Фильтр по проекту, 'all' = все"},
                    "days": {"type": "integer", "description": "Окно анализа в днях (по умолчанию 30)"},
                    "limit": {"type": "integer", "description": "Top-N в каждой секции (по умолчанию 10)"}
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
                    "top_k": {"type": "integer", "description": "Сколько кандидатов вернуть (default 3)"}
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
                    "auto_save": {"type": "boolean", "description": "true = сохранить как статьи, false = вернуть сводку для ревью"},
                    "group_by": {"type": "string", "enum": ["prefix", "branch", "file"], "description": "Группировка: prefix (conventional commits), branch, file (по директории)"},
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
                    "dry_run": {"type": "boolean", "description": "true = превью, false = импорт"},
                    "skip_inbox": {"type": "boolean", "description": "Пропустить папку Inbox (по умолчанию true)"}
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
                    "project": {"type": "string", "description": "Проект для сравнения (или 'all')"},
                    "days": {"type": "number", "description": "За сколько последних дней анализировать коммиты. Действует ТОЛЬКО с repo_path: при git_log_raw окно задано содержимым лога, и параметр молча игнорируется (по умолчанию 30)"},
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
                    "auto_save": {"type": "boolean", "description": "true = сохранить сразу, false = превью"}
                },
                "required": ["project"]
            }
        ),
    ]
    for t in tools:
        t.annotations = _annotations_for(t.name)
    # Маркер ПОСЛЕ локализации: иначе он лёг бы на русский текст и был бы затёрт
    # английским переводом описания. Служебный параметр — последним шагом.
    return _declare_client_session(
        _mark_required(_relax_project_requirement(localize_tools(tools))))


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
# ⚠️ В URI — ВЕРСИЯ СЕРВЕРА, и это не украшение: клиент забирает HTML вьюхи один
# раз и держит его на всю свою MCP-сессию, рестарты контейнера сквозь mcp-remote
# кэш не сбрасывают. Замер 2026-09-09: прод отдавал новую вьюху (зонд
# resources/read показывал маркер), а панель в чате рисовала старую — правки
# v1.74.2 в ней не существовало. Новая версия = новый URI = свежая загрузка;
# read_resource сравнивает ПУТЬ без query, так что старый URI из кэша тоже
# отвечает.
UI_SCHEME = "ui://"
UI_MIME = "text/html;profile=mcp-app"
UI_SEARCH_PATH = "ui://memory-compiler/search-results.html"
UI_SEARCH_RESOURCE = f"{UI_SEARCH_PATH}?v={config.VERSION}"


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
    # resources/read молчал (issue #3): по логам нельзя было узнать, запрашивал ли клиент
    # вьюху и по какому URI — единственный серверный след события «клиент перечитал вьюху».
    # Пишем строкой, как call_tool: uri + mime. Работает даже если до отрисовки не дошло.
    obs.new_request_id()
    obs.get_logger("resource").info(
        "resource read",
        extra={"uri": uri_s, "mime": UI_MIME if uri_s.startswith(UI_SCHEME) else _RESOURCE_MIME},
    )
    if uri_s.startswith(UI_SCHEME):
        # Вьюха MCP Apps. Отдаётся до всякой работы с базой: это статика, ни
        # проекта, ни файла тут нет, и путь в knowledge/ по ui:// не строится.
        from memory_compiler.ui_app import SEARCH_VIEW_HTML
        if uri_s.split("?", 1)[0] == UI_SEARCH_PATH:
            # Версия подставляется ЗДЕСЬ, где config.VERSION под рукой: SEARCH_VIEW_HTML —
            # raw-литерал (r\"\"\"), f-строкой его не сделать (пришлось бы экранировать все
            # {} внутри JS), поэтому плейсхолдер + .replace() (issue #3).
            html = SEARCH_VIEW_HTML.replace("__MC_VERSION__", config.VERSION)
            return [ReadResourceContents(content=html, mime_type=UI_MIME)]
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


# Имена полей, встречавшиеся в утёкших хвостах на боевой базе (замер 27.08.2026:
# tags 24, session_summary 19, open_questions 16). Голый тег ловим и по ним, а не
# только по параметрам ТЕКУЩЕГО инструмента: в daily хвост прилетает от разных
# вызовов, и `<session_summary>` попадает в статью, сохранённую save_lesson.
_LEAK_FIELDS = frozenset({"content", "session_summary", "open_questions", "tags", "decisions"})


def _leak_line_kind(line: str, key: str, others: set) -> tuple | None:
    """Опознать СТРОКУ хвоста вызова: ('drop',) или ('field', имя, значение, закрыта).

    ⚠️ Маркер обязан стоять В НАЧАЛЕ строки. Ровно это отличает хвост вызова от
    прозы про него: в статьях о самом баге разметка стоит внутри фразы, и такие
    упоминания трогать нельзя (см. негативные контроли в тестах).
    """
    s = line.strip()
    if s in (f"</{key}>", "</content>", _INVOKE_CLOSE, _PARAM_CLOSE):
        return ("drop",)
    m = _PARAM_OPEN_RE.match(s)
    if m and m.group(1) in others:
        raw = m.group(2)
        for close in (f"</{m.group(1)}>", _PARAM_CLOSE):
            if raw.endswith(close):
                return ("field", m.group(1), raw[:-len(close)], True)
        return ("field", m.group(1), raw, False)
    for q in others | _LEAK_FIELDS:
        if s.startswith(f"<{q}>"):
            raw = s[len(q) + 2:]
            close = f"</{q}>"
            if raw.endswith(close):
                return ("field", q, raw[:-len(close)], True)
            return ("field", q, raw, False)
    return None


def _strip_leaked_lines(text: str, key: str, others: set) -> tuple[str, dict]:
    """Снять строки хвоста вызова ГДЕ БЫ ОНИ НИ СТОЯЛИ, сохранив текст вокруг.

    Прежний guard шёл строго с конца (endswith/rpartition) и потому пропускал две
    формы: хвост посреди значения, за которым остался ещё абзац, и голые теги без
    обёртки `<parameter name=`, у которых нет якоря `</content>`.

    ⚠️ ТЕКСТ ВОКРУГ БЛОКА СОХРАНЯЕТСЯ, а не отрезается «до конца»: замер по базе
    27.08.2026 — в 51 случае из 56 после блока идёт содержательный текст, и в
    daily это следующие записи дня. Отрезав хвост целиком, чистка снесла бы их.
    """
    if text.count("```") % 2:
        return text, {}                    # незакрытый fenced-блок — не трогаем
    lines, out, fields = text.splitlines(), [], {}
    fence, pending, buf = False, None, []
    for line in lines:
        if line.lstrip().startswith("```"):
            fence = not fence
        if fence:
            out.append(line)
            continue
        if pending:                        # добираем многострочное значение поля
            close = f"</{pending}>"
            if close in line:
                buf.append(line.split(close)[0])
                fields.setdefault(pending, "\n".join(buf).strip())
                pending, buf = None, []
            else:
                buf.append(line)
            continue
        hit = _leak_line_kind(line, key, others)
        if hit is None:
            out.append(line)
        elif hit[0] == "drop":
            continue
        else:
            _, q, raw, closed = hit
            if closed:
                fields.setdefault(q, raw.strip())
            else:
                pending, buf = q, [raw]
    if pending:                            # блок так и не закрылся — берём как есть
        fields.setdefault(pending, "\n".join(buf).strip())
    return "\n".join(out).rstrip(), fields


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
            # Хвост стоит НЕ в конце (за ним остался текст) либо теги голые, без
            # обёртки `<parameter name=` — прежние правила такое не находят.
            # Разбираем построчно, сохраняя текст вокруг блока.
            stripped, extra = _strip_leaked_lines(text, key, set(props) - {key})
            if not extra and stripped == text:
                continue
            # ⚠️ Поле, которого у ЭТОГО инструмента нет, аргументом не отдаём —
            # вызов упадёт с TypeError (поймано живой проверкой v1.70.2:
            # session_summary из хвоста уехал в save_lesson). Обёртку снимаем, а
            # содержимое возвращаем в текст: знание не теряется, лежит прозой.
            orphan = [v for q, v in extra.items() if q not in props and v]
            fields = {q: v for q, v in extra.items() if q in props}
            if orphan:
                stripped = (stripped + "\n\n" + "\n".join(orphan)).strip()
            out[key] = stripped
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

    # Id чата от клиента (v1.76.0) — служебный аргумент, а не параметр
    # инструмента: хендлер упал бы на лишнем kwarg, аудиту он не нужен.
    # Используется только ключом свежести (_append_freshness).
    client_session = arguments.pop(freshness.CLIENT_SESSION_ARG, None)

    # ⚠️ ПРОПУЩЕННЫЙ `project` БЕРЁМ ИЗ ИСТОРИИ СЕССИИ (v1.90.0). Замер
    # 20.09.2026 по транскриптам: 78 отказов -32602 за 30 дней, и крупнейшая их
    # часть — именно он (finish_task 21, save_lesson 7, session_note 2). Корень
    # тот же, что у всего класса: клиент не показывает модели `required` у
    # строковых параметров, она читает поле как опциональное и опускает. Маркер
    # « (обязательно)» из v1.54.0 частоту снизил, но не убрал — падения идут и
    # в сентябре 2026, а каждое означает ПОТЕРЯННУЮ запись.
    # Критерий подстановки берётся из схемы (project в `required`), а не списком
    # руками: у search и search_by_tag он необязателен, там 'all' — осмысленный
    # режим «по всей базе», и подстановка молча сузила бы выдачу до проекта.
    substituted_project = ""
    if isinstance(arguments, dict) and not arguments.get("project") \
            and name in _PROJECT_FROM_SESSION:
        # ⚠️ Угадываем ТОЛЬКО когда ключ — id чата от клиента (c:…). У моста
        # Claude Desktop одна MCP-сессия на ВСЕ чаты Code (v1.76.0): по общему
        # ключу «последний проект» оказался бы проектом соседнего чата, и запись
        # молча уехала бы к нему. Лучше отказ.
        key = _session_key(client_session)
        substituted_project = freshness.last_project(key) if key.startswith("c:") else ""
        if substituted_project:
            arguments["project"] = substituted_project
        else:
            return [TextContent(type="text", text=(
                f"❌ Не указан `project`, и подставить его не из чего: эта сессия ещё не "
                f"открывала ни одного проекта. Назови проект явно — `{name}(project=\"…\", …)` "
                f"— или сперва загрузи контекст (`start_task`, `search`)."))]

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

    # ⚠️ ПРОЕКТА, КОТОРОГО НЕТ, ХЕНДЛЕР НЕ ВИДИТ. 14.09.2026 агент угадал
    # project="memorycompiler" по каталогу клона, start_task молча завёл пустой
    # двойник проекта memory-compiler, а тот потом сбивал route_project. Каталог
    # чтение больше не создаёт (storage.project_path), но пустой ответ «ничего не
    # найдено» по опечатке всё равно врёт: сессия решит, что знаний нет. Поэтому
    # чтение получает подсказку, а запись в двойника по ключу — отказ.
    # Сторож свежести при отказе не зовётся: он запомнил бы несуществующий проект
    # «последним проектом сессии», и следующая запись без project ушла бы туда.
    project = _named_project(arguments)
    existed = _project_state(project)
    refusal = _project_gate(name, project) if existed is False else ""
    if refusal:
        _log.info("project not found", extra={"tool": name, "project": project})
        if name == "search":
            handlers.search_payload_var.set(None)   # выдачу собирать не из чего

    # Ключ чата для хендлера (v1.92.0): start_task по нему показывает уже виденное
    # следом. ⚠️ Только вида c:<id> — у моста Claude Desktop одна MCP-сессия на все
    # чаты Code, и по общему ключу один чат прятал бы пункты от другого.
    chat_key = _session_key(client_session)
    chat_token = freshness.chat_key_var.set(chat_key if chat_key.startswith("c:") else "")
    try:
        result = ([TextContent(type="text", text=refusal)] if refusal
                  else await _dispatch_tool(name, arguments))
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
    finally:
        freshness.chat_key_var.reset(chat_token)
    if not refusal:
        # Свежесть контекста между сессиями: сервер знает про все записи, поэтому
        # может сам сказать этой сессии, что под ней изменилось. Считаем ДО audit_log
        # и до подсчёта размера — футер тоже часть ответа.
        result = _append_freshness(name, arguments, result, client_session)
        # Новый проект и пустой двойник называются вслух — отдельным блоком, как
        # футер свежести: обычные ответы остаются байт-в-байт прежними.
        note = _project_note(project, existed)
        if note:
            result = list(result) + [TextContent(type="text", text=note)]

    # ⚠️ Подставленный проект НАЗЫВАЕТСЯ ВСЛУХ: молча записать «куда-то» хуже,
    # чем отказать — сессия не узнает, что попала не туда, а исправлять придётся
    # руками. Отдельным блоком, как футер свежести: сотни ассертов сравнивают
    # тексты ответов дословно.
    if substituted_project:
        result = list(result) + [TextContent(type="text", text=(
            f"\n📁 `project` не был указан — записано в «{substituted_project}», "
            f"проект этой сессии."))]

    # У search одна форма выдачи (v1.87.0). Собираем её ДО подсчёта размера:
    # size в аудите обязан мерить то, что получит модель, а не спрятанный текст.
    structured = None
    if name == "search":
        result, structured = _search_response(arguments.get("query", ""), result)

    # Track response size (result может содержать ResourceLink без .text)
    total = sum(len(getattr(t, "text", "") or "") for t in result)
    stats["total_chars_returned"] = stats.get("total_chars_returned", 0) + total
    # Число найденного — аналитике (analytics.quality): у search без префикса
    # «score: » короткий, но непустой ответ по одной длине выглядит промахом.
    if name == "search" and structured is not None:
        audit_args = {**audit_args, "_count": structured.get("count")}
    audit_log(name, audit_args, total)
    # Маршрут вызова (v1.76.1): имя клиента из initialize и признак номера вызова
    # в _meta. По ним видно, доезжает ли claudecode/toolUseId через мост Claude
    # Desktop, — на этом стоит боковой канал «вызов → чат».
    origin = {"client": "", "tool_use_id": False}
    try:
        ctx = app.request_context
        extra = (ctx.meta.model_extra or {}) if ctx.meta is not None else {}
        origin["tool_use_id"] = bool(extra.get("claudecode/toolUseId"))
        params = ctx.session.client_params
        origin["client"] = params.clientInfo.name if params is not None else ""
    except Exception:
        pass                              # вне запроса (REST, тесты) — поля по умолчанию
    _log.info("tool ok", extra={"tool": name, "dur_ms": int((time.perf_counter() - t0) * 1000),
                                "size": total, **origin})
    # У search объявлен outputSchema — обязаны вернуть structuredContent (SDK валидирует).
    if structured is not None:
        return (result, structured)
    return result



def _merge_notice_into_payload(payload: dict, notice: str) -> dict:
    """Вписать футер в структурированную выдачу — с v1.87.0 это ЕДИНСТВЕННОЕ
    место, где он живёт у search: отдельного текстового блока для футера больше нет.

    Клиент с поддержкой outputSchema читает structuredContent и дополнительный
    TextContent модели не показывает — без этого подсказка не доезжает именно
    там, где она нужнее всего: `search` чаще всего и открывает «слепую» сессию.
    """
    if not notice or not isinstance(payload, dict):
        return payload
    out = dict(payload)
    out["notice"] = notice.strip()
    return out


def _search_response(query: str, blocks: list) -> tuple[list[TextContent], dict]:
    """Одна форма выдачи search (v1.87.0): structuredContent и ЕДИНСТВЕННЫЙ
    текстовый блок — один и тот же JSON.

    Claude Code при объявленном outputSchema показывает модели structuredContent,
    а текстовые блоки — нет (замер 15.09.2026: превью не дошло до модели ни в
    одном из 723 поисков). Поэтому футер свежести и ошибка параметра не могут
    жить отдельным блоком — они уходят в notice.
    """
    payload = _build_search_structured(query)
    extra = [str(getattr(b, "text", "") or "").strip() for b in blocks
             if getattr(b, "type", "") == "text"
             and not str(getattr(b, "text", "") or "").lstrip().startswith("{")]
    payload = _merge_notice_into_payload(payload, "\n\n".join(t for t in extra if t))
    return [TextContent(type="text", text=handlers.search_json(payload))], payload


# Инструменты, чья запись делает контекст ДРУГИХ сессий устаревшим.
_FRESHNESS_WRITE_TOOLS = {
    "save_lesson", "save_decision", "save_runbook", "save_secret", "save_tracking",
    "save_session", "save_from_template", "save_contexts", "save_compact",
    "finish_task", "edit_article", "delete_article", "consolidate", "ingest",
    "session_note",
}

# Вызовы, после которых проект становится рабочим для подстановки пропущенного
# project: сессия в него пишет или открыла по нему задачу. Чтение сюда не входит
# намеренно — справка из чужого проекта не должна уводить туда записи.
_WORK_PROJECT_TOOLS = _FRESHNESS_WRITE_TOOLS | {"start_task"}


# Инструменты, которые САМИ отдают контекст проекта: подсказка при первом
# обращении дублировала бы их выдачу.
_CONTEXT_TOOLS = {"start_task", "load_session", "get_active_context",
                  "open_questions", "get_context", "get_summary"}

# Справочные вызовы: читающие инструменты, кроме тех, что сами отдают контекст.
# Список выводится, а не ведётся руками, — новый читающий инструмент не выпадет.
# Справка из чужого проекта подсказку первого обращения не получает (v1.91.0).
# ⚠️ _READONLY_LOCAL — про MCP-аннотацию клиенту (readOnlyHint), а не про факт
# записи: consolidate туда попал по этой аннотации, хотя реально пишет и стоит
# в _FRESHNESS_WRITE_TOOLS. Без вычитания последнего он считался бы «справкой».
_LOOKUP_TOOLS = _READONLY_LOCAL - _CONTEXT_TOOLS - _FRESHNESS_WRITE_TOOLS


def _session_key(client_session: str | None) -> str:
    """Ключ снимка этой сессии: id чата от клиента, иначе объект MCP-сессии.

    Вне запроса (REST, тесты без моста) сессии нет — возвращаем пустой ключ, и
    подстановка проекта тихо отключается: гадать в таком режиме не на чем.
    """
    try:
        session = app.request_context.session
    except Exception:
        return ""
    return freshness.key_for(session, client_session)


def _named_project(arguments: dict) -> str:
    """Проект, который вызов назвал явно; '' — не назвал или просил «по всем» ('all')."""
    project = arguments.get("project") if isinstance(arguments, dict) else None
    if not isinstance(project, str) or not project or project == "all":
        return ""
    return project


def _project_state(project: str) -> bool | None:
    """Есть ли каталог проекта. None — проверять нечего: проект не назван или имя
    небезопасно (такое отвергнет сам хендлер своей ошибкой, как и раньше)."""
    if not project:
        return None
    try:
        return storage.safe_project_path(project).is_dir()
    except ValueError:
        return None


def _twin_hint(twins: list[tuple[str, int]]) -> str:
    """«memory-compiler» (статей: 37) — то же имя без учёта регистра…; прочие двойники — хвостом."""
    best, count = twins[0]
    text = (f"«{best}» (статей: {count}) — то же имя без учёта регистра, дефисов "
            f"и подчёркиваний")
    if len(twins) > 1:
        text += "; тот же ключ и у " + ", ".join(f"«{n}» (статей: {c})" for n, c in twins[1:])
    return text


def _project_gate(name: str, project: str) -> str:
    """Ответ ВМЕСТО вызова, когда названного проекта в базе нет; '' — вызов пускать.

    Чтению — подсказка: вызов не выполнен, каталог не заведён, и кого, скорее
    всего, имели в виду. Записи — отказ, только если есть двойник по ключу
    (memorycompiler при живом memory-compiler): так знания расползаются на два
    проекта. Без двойника запись заводит новый проект, как и раньше.

    ⚠️ ДВОЙНИКА НЕ ПОДСТАВЛЯЕМ, даже на записи. Подстановка v1.90.0 заполняет
    ПРОПУЩЕННЫЙ project, а здесь его назвали явно, и переписать явный выбор
    молча — хуже отказа: отказ стоит одного повтора, а запись не туда
    исправляют руками. Нужен именно отдельный проект — add_project, затем запись.
    """
    twins = storage.project_twins(project)
    best = twins[0][0] if twins else ""
    if name in _PROJECT_CREATORS:
        if not twins:
            return ""
        return (f"❌ Ничего не записано: проекта «{project}» в базе нет. Скорее всего, "
                f"имелся в виду {_twin_hint(twins)}. Повтори вызов с "
                f"`project=\"{best}\"`. Если нужен именно отдельный новый проект "
                f"«{project}», заведи его явно — `add_project(name=\"{project}\")` — "
                f"и повтори запись.")
    lines = [f"❌ Проекта «{project}» в базе нет: вызов не выполнен, каталог под него "
             f"не заведён."]
    if twins:
        lines.append(f"Возможно, имелся в виду {_twin_hint(twins)}. Повтори вызов с "
                     f"`project=\"{best}\"`.")
    else:
        near = storage.project_near_names(project)
        if near:
            lines.append("Похожие имена: " + ", ".join(f"«{n}»" for n in near) + ".")
        lines.append("Все проекты — `list_projects`, подбор по рабочему каталогу — "
                     "`route_project(cwd=…)`. Новый проект появляется с первой записью "
                     "(`save_lesson`, `finish_task`) или через `add_project`.")
    return "\n".join(lines)


def _project_note(project: str, existed: bool | None) -> str:
    """Сноска о проекте после вызова: только что заведён или похож на пустой двойник.

    Новый проект называется потому, что его заводит и опечатка (memory-compile):
    без сноски сессия не узнает, что записала мимо. Пустой двойник — проект без
    единой статьи при непустом тёзке по ключу: такие заводились сами, пока чтение
    делало mkdir, и доживают в базе до ручной уборки.
    """
    if existed is None or not _project_state(project):
        return ""
    if existed is False:
        note = f"\n📁 Заведён новый проект «{project}» — раньше его в базе не было."
        near = storage.project_near_names(project)
        if near:
            note += (" Похожие существующие: " + ", ".join(f"«{n}»" for n in near)
                     + " — если имелся в виду один из них, перенеси запись туда.")
        return note
    if storage.project_has_articles(project):
        return ""
    twins = [(n, c) for n, c in storage.project_twins(project) if c > 0]
    if not twins:
        return ""
    return (f"\n⚠️ В «{project}» нет ни одной статьи — похоже на двойник, заведённый "
            f"угаданным именем. Нужный проект, скорее всего, {_twin_hint(twins)}.")


def _append_freshness(name: str, arguments: dict, result: list,
                      client_session: str | None = None) -> list:
    """Дописать к ответу предупреждение о чужих записях в этом проекте.

    ⚠️ Отдельным блоком, а не приклейкой к существующему тексту: 414 ассертов в
    тестах сравнивают тексты ответов дословно, а отдельный TextContent появляется
    ТОЛЬКО когда есть что сказать, поэтому обычные ответы остаются байт-в-байт
    прежними. У search resource_link-блоков нет: отдельный футер для него
    сворачивает в notice `_search_response` (см. `_merge_notice_into_payload`).

    ⚠️ Ключ — id чата от клиента, если он пришёл (v1.76.0), и только потом объект
    MCP-сессии: у моста Claude Desktop одна сессия на все чаты Code.
    """
    try:
        session = app.request_context.session
    except Exception:
        return result                     # вне запроса (REST, тесты) — не мешаем
    key = freshness.key_for(session, client_session)
    project = arguments.get("project") if isinstance(arguments, dict) else None
    # ⚠️ Спрашиваем ДО consume: тот делает touch и признак первого касания стирает.
    # Рабочий проект — тем же порядком: после consume им стал бы этот же проект.
    elsewhere = (name in _LOOKUP_TOOLS and bool(project) and project != "all"
                 and freshness.last_project(key) not in ("", project))
    first = (freshness.is_first_touch(key, project or "")
             and name not in _CONTEXT_TOOLS and not elsewhere)
    try:
        # ⚠️ Своя запись — ДО consume: она сдвигает отсчёт молчания, а подсказку о нём
        # собирает consume. В обратном порядке ответ на session_note приходил с
        # напоминанием «больше 25 минут без записи в базу» (15.09.2026). Сноску о чужих
        # записях порядок не меняет: свои consume отфильтровывает по ключу.
        if name in _FRESHNESS_WRITE_TOOLS and project and project != "all":
            topic = ""
            if isinstance(arguments, dict):
                # у session_note нет ни topic, ни filename — иначе чужая сессия
                # увидит «session_note: (без темы)» и не поймёт, что изменилось
                topic = str(arguments.get("topic") or arguments.get("filename")
                            or arguments.get("note") or "")
            freshness.note_write(project, name, topic, key)
        if name in _WORK_PROJECT_TOOLS:
            freshness.claim(key, project or "")
        note = freshness.consume(key, project or "")
        if first:
            note = handlers.first_touch_context(project) + note
    except Exception:
        return result                     # сторож не имеет права ронять вызов
    if note:
        return list(result) + [TextContent(type="text", text=note)]
    return result


def _build_search_structured(query: str) -> dict:
    """Структурированная выдача search — payload, собранный самим хендлером.

    Сборки из resource-ссылок больше нет (v1.87.0): ссылок у search нет вовсе.
    Payload не нашёлся (хендлер не дошёл до сборки) — пустая выдача того же
    вида, а не чужой payload: панель иначе показала бы прошлый вызов.
    """
    payload = handlers.search_payload_var.get()
    if payload is not None and payload.get("query") == query:
        return payload
    return {"query": query, "count": 0, "results": []}


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
