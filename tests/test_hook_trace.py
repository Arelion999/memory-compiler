"""Behavior contract for local hook tracing; no live services or user config."""
import copy
import importlib.util
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "mc_guard.py"
UUID4 = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")


@pytest.fixture
def guard(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("isolated_guard", HOOK)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    g.TRACE_CONFIG_PATH = tmp_path / "trace.json"
    g.HOOK_LOG = tmp_path / "hooks.log"
    g.STATE_DIR = tmp_path / "state"
    g.PENDING_DIR = tmp_path / "pending"
    monkeypatch.delenv("MC_GUARD_CLIENT", raising=False)
    monkeypatch.setattr(g.sys, "argv", ["mc_guard.py"])
    return g


def event(**kw):
    return dict(session_id="private-chat-prefix-0123", tool_name="mcp__memory-compiler__save_lesson",
                tool_input={"b": 1, "a": {"я": "тест", "z": [1, 2]}}, **kw)


def logs(g):
    return [json.loads(s) for s in g.HOOK_LOG.read_text(encoding="utf-8").splitlines()]


def test_device_stable_across_adapters_calls_and_concurrent_first_use(guard):
    def metadata(i):
        return json.loads(guard.trace_metadata(family="claude" if i % 2 else "kimi"))
    with ThreadPoolExecutor(max_workers=12) as pool:
        traces = list(pool.map(metadata, range(36)))
    assert len({t["device"] for t in traces}) == 1
    assert len({t["call_id"] for t in traces}) == 36
    assert all(UUID4.fullmatch(t["device"]) and UUID4.fullmatch(t["call_id"]) for t in traces)
    assert all(set(t) == {"v", "device", "family", "call_id"} and type(t["v"]) is int and t["v"] == 1 for t in traces)
    # A separate core instance reads the winner's complete file.
    spec = importlib.util.spec_from_file_location("second_guard", HOOK)
    other = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(other)
    other.TRACE_CONFIG_PATH = guard.TRACE_CONFIG_PATH
    assert json.loads(other.trace_metadata())["device"] == traces[0]["device"]


@pytest.mark.parametrize("bad", ["not json", "x" * 4096, '{"device":"not-a-token"}',
                                  '{"device":"00000000000010008000000000000000"}',
                                  '{"v":true,"device":"00000000000040008000000000000000"}'])
def test_corrupt_config_fails_open_without_replacing_identity(guard, bad):
    guard.TRACE_CONFIG_PATH.write_text(bad, encoding="utf-8")
    assert guard.trace_metadata() == guard.trace_metadata() == ""
    assert guard.TRACE_CONFIG_PATH.read_text(encoding="utf-8") == bad


def test_unwritable_config_keeps_session_and_drops_forged_trace(guard, monkeypatch):
    guard.TRACE_CONFIG_PATH.parent.joinpath("blocked").write_text("file")
    guard.TRACE_CONFIG_PATH = guard.TRACE_CONFIG_PATH.parent / "blocked" / "trace.json"
    args = {"query": "body", "_client_session": "real-chat", "_client_trace": "forged"}
    result = guard.with_trace_arguments(args, family="codex")
    assert result == {"query": "body", "_client_session": "real-chat"}
    assert args["_client_trace"] == "forged"


def test_explicit_codex_family_does_not_change_gate_profile(guard):
    guard._CLIENT_CACHE = "claude"  # installed adapter's actual convention
    guard.TRACE_CLIENT_FAMILY = "codex"
    original = {"query": "body", "_client_session": "adapter-chat"}
    # Installed adapter imports a sibling core, forces the Claude gate profile,
    # injects session itself, and does not invoke cmd_session_arg.
    class CodexAdapter:
        def arguments(self, args):
            return guard.with_trace_arguments(dict(args, _client_session="adapter-chat"))

        def pre_tool_use(self, args):
            return {"hookSpecificOutput": {"updatedInput": self.arguments(args)}}

        def bootstrap(self):
            return [(name, self.arguments({})) for name in ("route_project", "get_summary")]

        def mcp_call(self, args):
            return self.arguments(args)

    adapter = CodexAdapter()
    paths = [adapter.pre_tool_use(original)["hookSpecificOutput"]["updatedInput"],
             *(args for _, args in adapter.bootstrap()), adapter.mcp_call(original)]
    assert all(json.loads(p["_client_trace"])["family"] == "codex" for p in paths)
    assert len({json.loads(p["_client_trace"])["call_id"] for p in paths}) == 4
    assert guard._CLIENT_CACHE == "claude" and "_client_trace" not in original


def test_family_comes_from_documented_prefix_before_normalization(guard):
    raw = {"tool_name": "mcp__plugin-memory-compiler_memory-compiler__search"}
    normalized = guard.normalize_event(raw)
    assert json.loads(guard.trace_metadata(normalized))["family"] == "kimiwork"
    assert json.loads(guard.trace_metadata({"client_type": "kimi"}))["family"] == "kimi"
    assert json.loads(guard.trace_metadata({"client_type": "claude"}))["family"] == "claude"
    assert json.loads(guard.trace_metadata({"agent_id": "not-a-client-marker"}))["family"] == "unknown"


@pytest.mark.parametrize("reply,success", [(None, False), (False, False), ({"ok": False}, False),
                                          ({"ok": 1}, False), ({"ok": True}, True), ("raise", False)])
def test_one_correlation_links_hint_updated_input_and_journal(guard, monkeypatch, reply, success):
    sent, emitted = [], []
    def post(path, payload, timeout):
        sent.append((path, payload))
        if reply == "raise":
            raise OSError("private-failure")
        return reply
    monkeypatch.setattr(guard, "_post_json", post)
    monkeypatch.setattr(guard, "emit", emitted.append)
    ev = event()
    ev["tool_input"].update(_client_session=ev["session_id"], _client_trace="old-private-metadata")
    before = copy.deepcopy(ev)
    assert guard.cmd_session_arg(ev) == 0
    assert ev == before
    upd = emitted[0]["hookSpecificOutput"]["updatedInput"]
    trace = json.loads(upd["_client_trace"])
    assert upd["_client_session"] == ev["session_id"]
    assert sent[0][1]["trace"] == upd["_client_trace"]
    assert sent[0][1]["fp"] == "0dab080e1acde7f350b12cae047758b895a460ee"
    rows = logs(guard)
    assert {r["correlation_id"] for r in rows} == {trace["call_id"]}
    assert ("session_arg.hint" in [r["action"] for r in rows]) is success
    assert ("session_arg.hint_failed" in [r["action"] for r in rows]) is not success


def test_queue_retry_signature_and_fallback_key_ignore_trace(guard):
    args = {"topic": "retry", "content": "same payload"}
    enriched = dict(args, _client_session="chat", _client_trace="one-call")
    assert guard._call_signature("save_lesson", args) == guard._call_signature("save_lesson", enriched)
    ev = dict(tool_name="save_lesson", tool_input=args)
    assert guard._pending_key(ev) == guard._pending_key(dict(ev, tool_input=enriched))


def test_journal_contains_only_pseudonyms_categories_and_fixed_reasons(guard):
    ev = event()
    guard.journal(ev, "gate.block", tool="private-command", detail="private body", reason="missing_or_stale_read")
    guard.journal(ev, "gate.block", reason="private reason")
    rows = logs(guard)
    text = guard.HOOK_LOG.read_text(encoding="utf-8")
    assert "private" not in text and "detail" not in text
    assert rows[0]["session"] == rows[1]["session"] and re.fullmatch("[0-9a-f]{64}", rows[0]["session"])
    assert rows[0]["reason"] == "missing_or_stale_read"
    assert "reason" not in rows[1]
    assert rows[0]["tool"] == "other"
    assert re.fullmatch("[0-9a-f]{64}", rows[0]["device_hash"])


def test_gate_effect_never_joins_unknown_but_reads_legacy_logs(guard):
    now = datetime.now()
    rows = []
    for sid in (None, "", "unknown", "old-hash"):
        rows.extend([{"action": "gate.block", "session": sid, "_ts": now},
                     {"action": "mc.ok", "tool": "search", "session": sid, "_ts": now + timedelta(seconds=1)}])
    assert guard._gate_effect(rows) == (1, 4)


@pytest.mark.parametrize("tool,reason", [("mcp__memory-compiler__search", "memory_tool"),
                                        ("Skill", "loader"), ("Read", "outside_scope")])
def test_gate_skips_have_reasons_without_changing_decisions(guard, monkeypatch, tool, reason):
    guard._CLIENT_CACHE = "claude"
    monkeypatch.setattr(guard, "emit", lambda _: pytest.fail("skip must not emit"))
    assert guard.cmd_gate(dict(tool_name=tool, session_id="gate-chat")) == 0
    assert logs(guard)[-1]["reason"] == reason


@pytest.mark.parametrize("state,known,action,reason,decision", [
    ({"last_read_ts": "fresh"}, False, "gate.pass", "fresh_read", None),
    ({}, True, "gate.card", "target_card", None),
    ({"blocks": 2}, False, "gate.valve", "block_limit", "allow"),
    ({}, False, "gate.block", "missing_or_stale_read", "deny"),
])
def test_gate_decisions_and_state_are_unchanged(guard, monkeypatch, state, known, action, reason, decision):
    guard._CLIENT_CACHE = "claude"
    emitted = []
    state = dict(state)
    if state.get("last_read_ts") == "fresh":
        state["last_read_ts"] = guard.time.time()
    monkeypatch.setattr(guard, "load_state", lambda _: state)
    monkeypatch.setattr(guard, "save_state", lambda *a: None)
    monkeypatch.setattr(guard, "_target_memo", lambda _: ("", known))
    monkeypatch.setattr(guard, "_target_hint", lambda _: "private-target")
    monkeypatch.setattr(guard, "emit", emitted.append)
    ev = {"session_id": "gate-chat", "tool_name": "mcp__ssh__execute", "tool_input": {}}
    assert guard.cmd_gate(ev) == 0
    assert logs(guard)[-1]["action"] == action and logs(guard)[-1]["reason"] == reason
    assert (emitted[-1]["hookSpecificOutput"]["permissionDecision"] if emitted else None) == decision
    assert "private-target" not in guard.HOOK_LOG.read_text(encoding="utf-8")
    if action in {"gate.pass", "gate.card"}:
        assert state["did_infra"] is True
    elif action == "gate.valve":
        assert state["blocks"] == 0
    else:
        assert state["blocks"] == 1


def test_metadata_failure_preserves_updated_input_and_hint(guard, monkeypatch):
    def fail(**kw):
        raise OSError("private failure")
    monkeypatch.setattr(guard, "trace_metadata", fail)
    sent, emitted = [], []
    monkeypatch.setattr(guard, "emit", emitted.append)
    monkeypatch.setattr(guard, "_post_json", lambda path, payload, timeout: sent.append(payload))
    ev = event()
    assert guard.cmd_session_arg(ev) == 0
    assert emitted[0]["hookSpecificOutput"]["updatedInput"]["_client_session"] == ev["session_id"]
    assert "_client_trace" not in emitted[0]["hookSpecificOutput"]["updatedInput"]
    assert sent[0]["chat_id"] == ev["session_id"]


def test_device_publish_failure_does_not_create_or_rotate_identity(guard, monkeypatch):
    def fail(*args, **kw):
        raise OSError("no links supported")
    monkeypatch.setattr(guard.os, "link", fail)
    assert guard.trace_metadata() == guard.trace_metadata() == ""
    assert not guard.TRACE_CONFIG_PATH.exists()
    assert not list(guard.TRACE_CONFIG_PATH.parent.glob(".trace-*"))


def test_kimiwork_infra_block_then_plugin_read_share_chat_identity(guard, monkeypatch):
    monkeypatch.setattr(guard.sys, "argv", ["mc_guard.py", "--client=kimi"])
    guard._CLIENT_CACHE = "kimi"
    monkeypatch.setattr(guard, "emit", lambda _: None)
    monkeypatch.setattr(guard, "_target_memo", lambda _: ("", False))
    monkeypatch.setattr(guard, "_target_hint", lambda _: "")
    infra = {"session_id": "one-work-chat", "tool_name": "mcp__ssh__execute", "tool_input": {}}
    read = guard.normalize_event({"session_id": infra["session_id"],
        "tool_name": "mcp__plugin-memory-compiler_memory-compiler__search", "tool_input": {}})
    assert guard.cmd_gate(infra) == 0
    guard.cmd_mark(read)
    rows = logs(guard)
    block = next(r for r in rows if r["action"] == "gate.block")
    success = next(r for r in rows if r["action"] == "mc.ok")
    assert (block["client_family"], success["client_family"]) == ("kimi", "kimiwork")
    for r in rows:
        r["_ts"] = datetime.strptime(r["ts"], guard.AUDIT_TS_FMT)
    assert guard._gate_effect(rows) == (1, 1)
    assert block["session"] == success["session"]
    guard.TRACE_CLIENT_FAMILY = "kimiwork"
    assert guard._trace_log_fields(infra)["session"] == block["session"]
    assert guard._trace_log_fields(infra)["client_family"] == "kimiwork"
