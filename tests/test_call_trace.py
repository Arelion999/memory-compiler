"""Safe attribution is metadata, never a handler argument or terminal payload."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from memory_compiler import freshness, obs, storage, tools


DEVICE = "123456781234423482341234567890ab"
TRACE = json.dumps({"v": 1, "device": DEVICE, "family": "codex"})


@pytest.fixture
def capture(monkeypatch, knowledge_dir):
    records = []
    logger = obs.get_logger("tool")

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    monkeypatch.setattr(tools, "app", SimpleNamespace(request_context=SimpleNamespace(
        session=SimpleNamespace(client_params=None), meta=None)))
    monkeypatch.setattr(tools, "_append_freshness", lambda n, a, r, c: r)
    freshness.reset()
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)
    freshness.reset()


def test_metadata_never_dispatches_and_audit_correlates(monkeypatch, capture):
    seen = []

    async def dispatch(name, args):
        seen.append(dict(args))
        return [TextContent(type="text", text="ok")]

    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    asyncio.run(tools.call_tool("read_article", {"project": "testproj", "filename": "x.md",
        "_client_session": "private-chat", "_client_trace": TRACE}))
    assert seen == [{"project": "testproj", "filename": "x.md"}]
    entries = storage.read_audit_log(10)
    assert len(entries) == 1
    trace = entries[0]["trace"]
    assert trace["client_family"] == "codex" and trace["chat_source"] == "explicit"
    assert len(trace["device_hash"]) == len(trace["chat_hash"]) == 64
    assert trace["device_hash"] != trace["chat_hash"]
    assert trace["outcome"] == "ok" and trace["reason"] == "completed"
    assert trace["dur_ms"] >= 0 and len(trace["request_id"]) == 12
    assert len(capture) == 1
    assert capture[0].request_id == trace["request_id"]
    serialized = json.dumps(entries) + obs.JsonLinesFormatter().format(capture[0])
    assert DEVICE not in serialized and "private-chat" not in serialized


@pytest.mark.parametrize("value", [None, {}, [], "broken", "x" * 10000,
    json.dumps({"v": True, "device": DEVICE, "family": "codex"}),
    json.dumps({"v": 1, "device": "a" * 32, "family": "codex"}),
    json.dumps({"v": 1, "device": DEVICE, "family": "secret-name"}),
    json.dumps({"v": 1, "device": DEVICE, "family": "codex", "extra": "private"})])
def test_malformed_trace_is_unknown_without_breaking_tool(value, monkeypatch, capture):
    async def dispatch(name, args):
        assert "_client_trace" not in args
        return [TextContent(type="text", text="ok")]
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    result = asyncio.run(tools.call_tool("read_article", {
        "project": "testproj", "filename": "x.md", "_client_trace": value}))
    assert result[0].text == "ok"
    trace = storage.read_audit_log(1)[0]["trace"]
    assert trace["device_hash"] == "" and trace["client_family"] == "unknown"


@pytest.mark.parametrize("mode,outcome,reason,error", [
    ("validation", "rejected", "argument_validation", "ArgumentValidationError"),
    ("nonobject", "rejected", "argument_validation", "ArgumentValidationError"),
    ("refusal", "rejected", "project_not_found", None),
    ("unsafe", "rejected", "unsafe_parameter", None),
    ("handler", "error", "handler_exception", "RuntimeError"),
    ("postprocess", "error", "postprocess_exception", "RuntimeError"),
    ("cancel", "cancelled", "cancelled", "CancelledError"),
])
def test_every_terminal_path_audited_once(mode, outcome, reason, error, monkeypatch, capture):
    async def dispatch(name, args):
        if mode == "handler":
            raise RuntimeError("secret-exception")
        if mode == "unsafe":
            raise ValueError("secret-exception")
        if mode == "cancel":
            raise asyncio.CancelledError()
        return [TextContent(type="text", text="ok")]
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    if mode == "postprocess":
        def fail(*a):
            raise RuntimeError("secret-exception")
        monkeypatch.setattr(tools, "_append_freshness", fail)
    args = {"project": "missing-project" if mode == "refusal" else "testproj", "filename": "x.md"}
    if mode == "validation":
        args["filename"] = []
    if mode == "nonobject":
        args = None
    async def run():
        token = obs.request_id_var.set("outer")
        try:
            try:
                await tools.call_tool("read_article", args)
            except (RuntimeError, asyncio.CancelledError):
                assert mode in ("handler", "postprocess", "cancel")
            assert obs.request_id_var.get() == "outer"
        finally:
            obs.request_id_var.reset(token)
    asyncio.run(run())
    entries = storage.read_audit_log(10)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.get("error") == error
    assert entry["trace"]["outcome"] == outcome and entry["trace"]["reason"] == reason
    terminal = [r for r in capture if r.getMessage() == "tool finished"]
    assert len(terminal) == 1
    if error:
        assert terminal[0].levelno == logging.ERROR
    assert "secret-exception" not in " ".join(obs.JsonLinesFormatter().format(r) for r in capture)


def test_direct_audit_strips_metadata(knowledge_dir):
    storage.audit_log("search", {"query": "q", "_client_session": "secret-chat", "_client_trace": TRACE}, 1)
    assert storage.read_audit_log(1)[0]["args"] == {"query": "q"}


def test_heal_does_not_restore_metadata():
    props = asyncio.run(tools._tool_props())["search"]
    args, _ = tools.heal_arguments({"query": 'q</query><_client_session>injected</_client_session>'}, props)
    assert "_client_session" not in args


def test_explicit_call_consumes_own_hint(monkeypatch, capture):
    seen = []
    async def dispatch(name, args):
        seen.append(freshness.chat_key_var.get())
        return [TextContent(type="text", text="ok")]
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    args = {"project": "testproj", "filename": "x.md"}
    freshness.hint_put(freshness.call_fingerprint("read_article", args), "hint-chat")
    asyncio.run(tools.call_tool("read_article", {**args, "_client_session": "explicit-chat"}))
    asyncio.run(tools.call_tool("read_article", dict(args)))
    assert seen == ["c:explicit-chat", ""]


def test_hash_domain_separation_and_key(monkeypatch):
    from memory_compiler import telemetry
    monkeypatch.setenv("MC_TRACE_KEY", "fixed-test-key")
    a = telemetry.identity_hash("device", DEVICE)
    assert a == telemetry.identity_hash("device", DEVICE)
    assert a != telemetry.identity_hash("chat", DEVICE)
    monkeypatch.setenv("MC_TRACE_KEY", "another-test-key")
    assert a != telemetry.identity_hash("device", DEVICE)


def test_schema_declares_trace_for_both_languages(monkeypatch):
    for lang in ("ru", "en"):
        monkeypatch.setattr(tools.i18n, "MC_LANG", lang)
        for tool in asyncio.run(tools.list_tools()):
            prop = tool.inputSchema["properties"]["_client_trace"]
            assert prop["type"] == "string" and prop["description"]
            assert "default" not in prop
            assert "_client_trace" not in tool.inputSchema.get("required", [])


def test_call_id_is_correlation_not_server_request_id(monkeypatch, capture):
    async def dispatch(name, args):
        return [TextContent(type="text", text="ok")]
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    payload = json.loads(TRACE) | {"call_id": "223456781234423482341234567890ab"}
    asyncio.run(tools.call_tool("read_article", {"project": "testproj", "filename": "x.md",
        "_client_trace": json.dumps(payload)}))
    trace = storage.read_audit_log(1)[0]["trace"]
    assert trace["correlation_id"] == payload["call_id"]
    assert trace["request_id"] != payload["call_id"]
    assert trace["client_family"] == "codex"


@pytest.mark.parametrize("call_id", ["private-call", "a" * 32, [], None, DEVICE.upper()])
def test_invalid_call_id_rejects_trace(call_id):
    from memory_compiler import telemetry
    assert telemetry.parse_trace(json.dumps(json.loads(TRACE) | {"call_id": call_id})) is None


def test_duplicate_trace_keys_rejected():
    from memory_compiler import telemetry
    assert telemetry.parse_trace(TRACE[:-1] + ', "family":"kimi"}') is None


def test_hint_trace_reaches_audit_via_authenticated_endpoint(monkeypatch, capture):
    from tests.test_session_hint import JsonRequest
    from memory_compiler import api
    monkeypatch.setattr(api, "MC_API_KEY", "test-key")
    async def dispatch(name, args):
        assert freshness.chat_key_var.get() == "c:hint-chat"
        return [TextContent(type="text", text="ok")]
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    args = {"project": "testproj", "filename": "x.md"}
    request = JsonRequest({"fp": freshness.call_fingerprint("read_article", args),
                           "chat_id": "hint-chat", "trace": TRACE}, auth="test-key")
    assert asyncio.run(api.web_session_hint(request)).status_code == 200
    asyncio.run(tools.call_tool("read_article", args))
    trace = storage.read_audit_log(1)[0]["trace"]
    assert trace["chat_source"] == "hint" and trace["client_family"] == "codex"


def test_unknown_initialize_and_tool_names_not_in_logs(monkeypatch, capture, caplog):
    import mcp.types as types
    params = types.InitializeRequestParams.model_validate({
        "protocolVersion": "2025-11-25",
        "clientInfo": {"name": "sentinel-private-codex-name", "version": "sentinel-private-version"},
        "capabilities": {"extensions": {"sentinel-private-extension": {}}}})
    tools.app.request_context.session.client_params = params
    tools._seen_clients.clear()
    caplog.set_level(logging.INFO, logger="mc.client")
    async def dispatch(name, args):
        return [TextContent(type="text", text="ok")]
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    asyncio.run(tools.call_tool("sentinel-private-tool", {}))
    serialized = " ".join(obs.JsonLinesFormatter().format(r) for r in capture + caplog.records)
    assert "sentinel-private" not in serialized
    assert capture[0].tool == "unknown" and capture[0].client_family == "unknown"


def test_trace_context_isolated_between_concurrent_calls(monkeypatch, capture):
    from memory_compiler import telemetry
    seen = []
    async def dispatch(name, args):
        before = dict(telemetry.trace_var.get())
        await asyncio.sleep(0)
        assert telemetry.trace_var.get() == before
        seen.append(before)
        return [TextContent(type="text", text="ok")]
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    async def run():
        outer = {"sentinel": True}
        token = telemetry.trace_var.set(outer)
        try:
            await asyncio.gather(*(tools.call_tool("read_article", {
                "project": "testproj", "filename": "x.md", "_client_session": chat,
                "_client_trace": TRACE}) for chat in ("chat-a", "chat-b")))
            assert telemetry.trace_var.get() is outer
        finally:
            telemetry.trace_var.reset(token)
    asyncio.run(run())
    assert len({t["request_id"] for t in seen}) == 2
    assert len({t["chat_hash"] for t in seen}) == 2
    assert len({t["device_hash"] for t in seen}) == 1
    assert len(storage.read_audit_log(10)) == 2


def test_audit_failure_does_not_break_tool(monkeypatch, capture):
    async def dispatch(name, args):
        return [TextContent(type="text", text="ok")]
    def failed_audit(*args, **kwargs):
        raise OSError("private-error")
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    monkeypatch.setattr(tools, "audit_log", failed_audit)
    result = asyncio.run(tools.call_tool("read_article", {"project": "testproj", "filename": "x.md"}))
    assert result[0].text == "ok" and len(capture) == 1


def test_explicit_trace_does_not_inherit_chat_from_different_device_hint(monkeypatch, capture):
    async def dispatch(name, args):
        assert freshness.chat_key_var.get() == ""
        return [TextContent(type="text", text="ok")]
    def append_freshness(name, args, result, client_session):
        assert client_session is None
        return result
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    monkeypatch.setattr(tools, "_append_freshness", append_freshness)
    args = {"project": "testproj", "filename": "x.md"}
    other = json.loads(TRACE) | {"device": "223456781234423482341234567890ab"}
    freshness.hint_put(freshness.call_fingerprint("read_article", args), "other-chat", json.dumps(other))
    asyncio.run(tools.call_tool("read_article", args | {"_client_trace": TRACE}))
    trace = storage.read_audit_log(1)[0]["trace"]
    assert trace["client_family"] == "codex" and trace["device_hash"]
    assert trace["chat_source"] == "conflict" and trace["chat_hash"] == ""


def test_hmac_failure_is_fail_open(monkeypatch, capture):
    from memory_compiler import telemetry
    async def dispatch(name, args):
        return [TextContent(type="text", text="ok")]
    def fail(*args):
        raise RuntimeError("private-error")
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    monkeypatch.setattr(telemetry, "identity_hash", fail)
    result = asyncio.run(tools.call_tool("read_article", {
        "project": "testproj", "filename": "x.md", "_client_trace": TRACE}))
    assert result[0].text == "ok"
    assert storage.read_audit_log(1)[0]["trace"]["device_hash"] == ""


def test_trace_length_boundary_and_missing_call_id():
    from memory_compiler import telemetry
    assert telemetry.parse_trace(TRACE + " " * (320 - len(TRACE))) == json.loads(TRACE)
    assert telemetry.parse_trace(TRACE + " " * (321 - len(TRACE))) is None


def test_matching_explicit_chat_can_use_trace_from_hint(monkeypatch, capture):
    async def dispatch(name, args):
        return [TextContent(type="text", text="ok")]
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    args = {"project": "testproj", "filename": "x.md"}
    freshness.hint_put(freshness.call_fingerprint("read_article", args), "own-chat", TRACE)
    asyncio.run(tools.call_tool("read_article", args | {"_client_session": "own-chat"}))
    trace = storage.read_audit_log(1)[0]["trace"]
    assert trace["client_family"] == "codex" and trace["device_hash"]
    assert trace["chat_source"] == "explicit"


@pytest.mark.parametrize("hash_failure", [False, True])
def test_conflicting_hint_cannot_choose_foreign_project_for_write(hash_failure, monkeypatch, capture, knowledge_dir):
    from unittest.mock import AsyncMock
    from mcp.types import CallToolResult
    from memory_compiler import telemetry
    args = {"topic": "must not write", "content": "body"}
    other = json.loads(TRACE) | {"device": "223456781234423482341234567890ab"}
    freshness.claim("c:foreign-chat", "testproj")
    freshness.hint_put(freshness.call_fingerprint("save_lesson", args), "foreign-chat", json.dumps(other))
    dispatch = AsyncMock(return_value=[TextContent(type="text", text="saved")])
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    if hash_failure:
        def fail(*args):
            raise RuntimeError("private-hmac-error")
        monkeypatch.setattr(telemetry, "identity_hash", fail)
    before = sorted(knowledge_dir.rglob("*.md"))
    result = asyncio.run(tools.call_tool("save_lesson", args | {"_client_trace": TRACE}))
    assert isinstance(result, CallToolResult) and result.isError
    assert "не указан `project`" in result.content[0].text
    dispatch.assert_not_awaited()
    assert sorted(knowledge_dir.rglob("*.md")) == before
    assert storage.read_audit_log(1)[0]["error"] == "ArgumentValidationError"


@pytest.mark.parametrize("name,args", [
    ("search", {"query": "cancelled-query"}),
    ("read_article", {"project": "testproj", "filename": "x.md"}),
    ("save_lesson", {"project": "testproj", "topic": "cancelled-write", "content": "body"}),
])
def test_cancellation_has_no_retrieval_success_or_miss(name, args, monkeypatch, capture, knowledge_dir):
    from memory_compiler import analytics
    monkeypatch.setattr(analytics, "KNOWLEDGE_DIR", knowledge_dir)
    if name != "search":
        storage.audit_log("search", {"query": "successful-query", "_count": 1}, 100)
    async def dispatch(name, args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(tools, "_dispatch_tool", dispatch)
    before = obs.stats()["total_errors"]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tools.call_tool(name, args))
    entry = storage.read_audit_log(1)[0]
    assert entry["error"] == "CancelledError"
    assert entry["trace"]["outcome"] == "cancelled"
    assert obs.stats()["total_errors"] == before + 1
    quality = analytics.quality(1)
    assert quality["successful_searches"] == (0 if name == "search" else 1)
    assert quality["search_errors"] == (1 if name == "search" else 0)
    assert quality["misses"] == quality["acted"] == quality["writes"] == 0
