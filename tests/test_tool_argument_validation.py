"""Argument failures are tool errors before handlers run, including through SDK."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent
from memory_compiler import tools


@pytest.mark.parametrize("name,args", [
    ("search", {"query": "private-value", "limit": 3}),
    ("search", {"query": "q", "private-value": "anything"}),
    ("finish_task", {"project": "testproj", "topic": "t", "content": "c", "verified": True}),
    ("search", {}),
    ("finish_task", {"project": 0, "topic": "t", "content": "c"}),
    ("search", {"query": ["private-value"]}),
    ("finish_task", {"project": "testproj", "topic": "t", "content": "c", "tags": [23]}),
    ("save_tracking", {"project": "testproj", "entity": "e", "facts": ["private-value"]}),
])
def test_invalid_arguments_never_dispatch(name, args, monkeypatch, knowledge_dir):
    dispatch = AsyncMock()
    audit = []
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    monkeypatch.setattr(tools, "audit_log", lambda *a, **kw: audit.append((a, kw)))
    before = sorted(knowledge_dir.rglob("*.md"))
    result = asyncio.run(tools.call_tool(name, dict(args)))
    assert isinstance(result, CallToolResult) and result.isError
    text = " ".join(c.text for c in result.content)
    assert "private-value" not in text
    assert "argument" in text.lower()
    if "limit" in args:
        assert "limit" in text
    if "verified" in args:
        assert "verified" in text
    if isinstance(args.get("query"), list) or isinstance(args.get("tags"), list):
        assert "string" in text
    dispatch.assert_not_awaited()
    assert audit[-1][1]["error"] == "ArgumentValidationError"
    assert "private-value" not in json.dumps(audit)
    assert sorted(knowledge_dir.rglob("*.md")) == before


@pytest.mark.parametrize("args", [{"query": "private-value", "limit": 1}, {"query": ["private-value"]}, {}])
def test_sdk_wrapper_preserves_sanitized_error(args, monkeypatch, knowledge_dir):
    dispatch = AsyncMock()
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    request = CallToolRequest(params=CallToolRequestParams(name="search", arguments=args))
    result = asyncio.run(tools.app.request_handlers[CallToolRequest](request)).root
    assert result.isError and result.structuredContent is None
    text = " ".join(c.text for c in result.content)
    assert "private-value" not in text and "Output validation" not in text
    assert "argument" in text.lower()
    dispatch.assert_not_awaited()


def test_internal_typeerror_remains_internal(monkeypatch, knowledge_dir):
    monkeypatch.setattr(tools, "_dispatch_tool", AsyncMock(side_effect=TypeError("internal failure")))
    with pytest.raises(TypeError, match="internal failure"):
        asyncio.run(tools.call_tool("search", {"query": "q"}))


@pytest.mark.parametrize("name,args", [
    ("save_tracking", {"project": "testproj", "entity": "e", "facts": "a note"}),
    ("finish_task", {"project": "testproj", "topic": "t", "content": "c", "tags": None}),
    ("save_lesson", {"project": "testproj", "content": "body\n<topic>healed topic</topic>"}),
])
def test_compatible_arguments_still_dispatch(name, args, monkeypatch, knowledge_dir):
    dispatch = AsyncMock(return_value=[TextContent(type="text", text="ok")])
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    asyncio.run(tools.call_tool(name, {**args, "_client_session": "test-chat"}))
    dispatch.assert_awaited_once()
    actual = dispatch.call_args.args[1]
    assert "_client_session" not in actual
    if name == "save_lesson":
        assert actual["topic"] == "healed topic"


def test_sdk_wrapper_heals_required_field_before_validation(monkeypatch, knowledge_dir):
    dispatch = AsyncMock(return_value=[TextContent(type="text", text="saved")])
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    request = CallToolRequest(params=CallToolRequestParams(name="save_lesson", arguments={
        "project": "testproj", "content": "body\n<topic>restored</topic>"}))
    result = asyncio.run(tools.app.request_handlers[CallToolRequest](request)).root
    assert not result.isError
    assert dispatch.call_args.args[1]["topic"] == "restored"


def test_sdk_wrapper_search_still_has_structured_content(knowledge_dir):
    request = CallToolRequest(params=CallToolRequestParams(
        name="search", arguments={"query": "docker", "project": "testproj"}))
    result = asyncio.run(tools.app.request_handlers[CallToolRequest](request)).root
    assert not result.isError
    assert result.structuredContent["count"] >= 1
    assert json.loads(result.content[0].text) == result.structuredContent


def test_malformed_service_session_keeps_historical_fallback(monkeypatch, knowledge_dir):
    dispatch = AsyncMock(return_value=[TextContent(type="text", text="ok")])
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    result = asyncio.run(tools.call_tool("save_tracking", {
        "project": "testproj", "entity": "e", "facts": "note",
        "_client_session": ["malformed"]}))
    assert not isinstance(result, CallToolResult)
    dispatch.assert_awaited_once()
    assert "_client_session" not in dispatch.call_args.args[1]


@pytest.mark.parametrize("arguments", [[], None, "private-value", 7])
def test_non_object_arguments_are_audited_errors(arguments, monkeypatch):
    dispatch = AsyncMock()
    audit = []
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    monkeypatch.setattr(tools, "audit_log", lambda *a, **kw: audit.append((a, kw)))
    result = asyncio.run(tools.call_tool("search", arguments))
    assert isinstance(result, CallToolResult) and result.isError
    text = " ".join(c.text for c in result.content)
    assert "object" in text and "private-value" not in text
    dispatch.assert_not_awaited()
    assert audit == [(("search", {}, 0), {"error": "ArgumentValidationError"})]


@pytest.mark.parametrize("mock_first", [True, False])
def test_validator_cache_is_isolated_between_cases(mock_first, monkeypatch):
    # The first case intentionally builds signatures from a handler mock. The
    # following case must independently derive nullable defaults from the handler.
    assert tools._TOOL_VALIDATORS is None
    if mock_first:
        monkeypatch.setattr(tools.handlers, "finish_task", AsyncMock())
    error = asyncio.run(tools._argument_error("finish_task", {
        "project": "testproj", "topic": "t", "content": "c", "tags": None}))
    if not mock_first:
        assert error == ""


@pytest.mark.parametrize("arguments", [
    {"project": "testproj", "topic": "t", "content": "c", "verified": True},
    {"topic": "t", "content": "c"},
    None,
])
def test_actual_argument_error_is_recognized_by_pending_hook(arguments, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "mc_guard_argument_integration", Path(__file__).resolve().parents[1] / "hooks" / "mc_guard.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    dispatch = AsyncMock()
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    result = asyncio.run(tools.call_tool("finish_task", arguments))
    assert result.isError
    text = " ".join(c.text for c in result.content)
    assert hook._args_rejected({"last_error": text}), text
    assert text.startswith("Input validation error: ")
    dispatch.assert_not_awaited()
