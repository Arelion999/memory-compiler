"""Tests for MCP tool annotations (P0: readOnly/destructive/idempotent/openWorld hints).

Аннотации дают Claude Desktop подсказки: read-only авто-подтверждаются (меньше трения
на каждый поиск), destructive требуют явного согласия, openWorld сигналят о внешних
источниках. Классификация статична (per tool, не per args), поэтому «может мутировать» =
readOnlyHint=False, даже если дефолтные аргументы только читают (lint fix=False, compile dry_run=True).
"""
import asyncio

from memory_compiler.tools import list_tools


def _by_name():
    tools = asyncio.run(list_tools())
    return {t.name: t for t in tools}


def test_every_tool_has_annotations():
    """Каждый tool должен получить аннотацию — иначе клиент трактует как неизвестное."""
    tools = asyncio.run(list_tools())
    missing = [t.name for t in tools if t.annotations is None]
    assert not missing, f"tools без аннотаций: {missing}"


def test_readonly_tools_marked_readonly():
    tools = _by_name()
    readonly = [
        "get_context", "search", "load_session", "get_summary", "ask",
        "get_active_context", "read_article", "search_by_tag", "article_history",
        "list_projects", "search_snippets", "get_runbook", "search_error",
        "get_project_deps", "search_decisions", "list_templates", "get_current",
        "consolidate", "stale_facts", "gap_report", "route_project", "knowledge_gap",
    ]
    for name in readonly:
        assert tools[name].annotations.readOnlyHint is True, f"{name} должен быть readOnly"
        # read-only не может быть деструктивным
        assert tools[name].annotations.destructiveHint is not True, f"{name} readOnly, но destructive"


def test_destructive_tools_marked():
    tools = _by_name()
    for name in ("delete_article", "remove_project"):
        ann = tools[name].annotations
        assert ann.readOnlyHint is False, f"{name} мутирует — не readOnly"
        assert ann.destructiveHint is True, f"{name} должен быть destructive"


def test_idempotent_tools_marked():
    tools = _by_name()
    for name in ("reindex", "init_schema"):
        ann = tools[name].annotations
        assert ann.readOnlyHint is False
        assert ann.idempotentHint is True, f"{name} должен быть idempotent"


def test_openworld_tools_marked():
    tools = _by_name()
    for name in ("ingest", "import_obsidian", "git_capture"):
        ann = tools[name].annotations
        assert ann.openWorldHint is True, f"{name} тянет внешний источник — openWorld"
        assert ann.readOnlyHint is False, f"{name} пишет в базу — не readOnly"


def test_write_tools_not_readonly():
    tools = _by_name()
    for name in ("save_lesson", "edit_article", "save_session", "compile", "lint",
                 "start_task", "finish_task", "save_secret", "save_decision"):
        assert tools[name].annotations.readOnlyHint is False, f"{name} мутирует — readOnly должен быть False"


def test_local_readonly_not_openworld():
    """Локальные read-tools не должны сигналить openWorld (только knowledge_gap читает внешнее)."""
    tools = _by_name()
    assert tools["search"].annotations.openWorldHint is False
    assert tools["read_article"].annotations.openWorldHint is False
    assert tools["knowledge_gap"].annotations.openWorldHint is True
