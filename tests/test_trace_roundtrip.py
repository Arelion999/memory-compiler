"""Exercise real hook metadata, authenticated hint endpoint and server dispatch."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from memory_compiler import api, freshness, storage, tools
from tests.test_session_hint import JsonRequest


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "roundtrip_guard", Path(__file__).resolve().parents[1] / "hooks" / "mc_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.TRACE_CONFIG_PATH = tmp_path / "trace.json"
    guard.HOOK_LOG = tmp_path / "hooks.log"
    guard.STATE_DIR = tmp_path / "state"
    guard.PENDING_DIR = tmp_path / "pending"
    emitted, delivered = [], []
    monkeypatch.setattr(guard, "emit", emitted.append)
    monkeypatch.setattr(api, "MC_API_KEY", "test-key")
    monkeypatch.setattr(tools, "app", SimpleNamespace(request_context=SimpleNamespace(
        session=SimpleNamespace(client_params=None), meta=None)))
    freshness.reset()

    def post(path, payload, timeout):
        assert path == "/api/session_hint" and timeout == 2
        delivered.append(payload)
        response = asyncio.run(api.web_session_hint(JsonRequest(payload, auth="test-key")))
        assert response.status_code == 200
        return json.loads(response.body)

    monkeypatch.setattr(guard, "_post_json", post)
    yield guard, emitted, delivered
    freshness.reset()


@pytest.mark.parametrize("family,via_hint", [
    ("codex", False), ("claude", False), ("kimi", True), ("kimiwork", True)])
def test_local_correlation_survives_real_dispatch_and_hint_consumption(
        bridge, monkeypatch, family, via_hint):
    guard, emitted, delivered = bridge
    guard._CLIENT_CACHE = "kimi" if family.startswith("kimi") else "claude"
    guard.TRACE_CLIENT_FAMILY = family
    original = {"project": "testproj", "filename": "x.md"}
    event = {"session_id": "private-roundtrip-chat", "tool_input": dict(original),
             "tool_name": ("mcp__plugin-memory-compiler_memory-compiler__read_article"
                           if family == "kimiwork" else "mcp__memory-compiler__read_article")}
    if family == "codex":
        # The external adapter injects session itself and does not call session_arg.
        updated = guard.with_trace_arguments(
            dict(original, _client_session=event["session_id"]))
        guard.journal(event, "session_arg.add", trace=updated["_client_trace"])
        assert not delivered
    else:
        guard.cmd_session_arg(event)
        updated = emitted[0]["hookSpecificOutput"]["updatedInput"]
        assert delivered[0]["trace"] == updated["_client_trace"]
        assert delivered[0]["fp"] == freshness.call_fingerprint("read_article", updated)
    metadata = json.loads(updated["_client_trace"])
    seen = []

    async def dispatch(name, args):
        seen.append((dict(args), freshness.chat_key_var.get()))
        return [TextContent(type="text", text="ok")]

    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    result = asyncio.run(tools.call_tool("read_article", original if via_hint else updated))
    assert result[0].text == "ok"
    assert event["tool_input"] == original
    assert seen == [(original, "c:" + event["session_id"])]
    row = storage.read_audit_log(1)[0]
    assert row["trace"]["client_family"] == family
    assert row["trace"]["chat_source"] == ("hint" if via_hint else "explicit")
    assert row["trace"]["correlation_id"] == metadata["call_id"]
    local = [json.loads(line) for line in guard.HOOK_LOG.read_text(encoding="utf-8").splitlines()]
    assert {entry["correlation_id"] for entry in local} == {metadata["call_id"]}
    serialized = json.dumps(row) + json.dumps(local)
    assert event["session_id"] not in serialized
    assert metadata["device"] not in serialized
    assert "_client_trace" not in row["args"] and "_client_session" not in row["args"]
    # A direct call must consume its hint too; the next call cannot inherit that chat.
    asyncio.run(tools.call_tool("read_article", dict(original)))
    assert seen[1][1] == ""
    assert storage.read_audit_log(1)[0]["trace"]["correlation_id"] == ""


def test_conflicting_real_hook_hints_cannot_select_foreign_write_project(bridge, monkeypatch):
    guard, _, _ = bridge
    guard._CLIENT_CACHE = "kimi"
    guard.TRACE_CLIENT_FAMILY = "kimi"
    original = {"topic": "diagnostic", "content": "safe body"}
    for sid in ("private-first-chat", "private-second-chat"):
        freshness.claim("c:" + sid, "testproj")
        guard.cmd_session_arg({"session_id": sid, "tool_input": dict(original),
                               "tool_name": "mcp__memory-compiler__save_lesson"})
    dispatch = AsyncMock()
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    result = asyncio.run(tools.call_tool("save_lesson", dict(original)))
    assert isinstance(result, CallToolResult) and result.isError
    dispatch.assert_not_awaited()
    row = storage.read_audit_log(1)[0]
    assert row["error"] == "ArgumentValidationError"
    assert row["trace"]["chat_source"] == "conflict"
    assert row["trace"]["chat_hash"] == row["trace"]["correlation_id"] == ""
