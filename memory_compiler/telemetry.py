"""Bounded, pseudonymous call attribution. Client metadata is never trusted text."""
from contextvars import ContextVar
import hashlib
import hmac
import json
import os
import re
import secrets

from memory_compiler import config

CLIENT_TRACE_ARG = "_client_trace"
SERVICE_ARGS = frozenset({"_client_session", CLIENT_TRACE_ARG})
FAMILIES = frozenset({"codex", "claude", "kimi", "kimiwork", "unknown"})
_DEVICE_RE = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")
_PROCESS_KEY = secrets.token_bytes(32)
MISSING = object()
trace_var: ContextVar[dict | None] = ContextVar("mc_call_trace", default=None)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def parse_trace(raw) -> dict | None:
    """Malformed metadata fails open; only the versioned exact schema is accepted."""
    if not isinstance(raw, str) or len(raw) > 320:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or not {"v", "device", "family"} <= set(value):
            return None
        if set(value) - {"v", "device", "family", "call_id"}:
            return None
        if type(value["v"]) is not int or value["v"] != 1:
            return None
        if not isinstance(value["device"], str) or not _DEVICE_RE.fullmatch(value["device"]):
            return None
        if not isinstance(value["family"], str) or value["family"] not in FAMILIES:
            return None
        if "call_id" in value and (not isinstance(value["call_id"], str)
                                   or not _DEVICE_RE.fullmatch(value["call_id"])):
            return None
        return value
    except (ValueError, TypeError, RecursionError):
        return None


def client_family(name) -> str:
    """Exact initialize-name allowlist; arbitrary substrings cannot identify clients."""
    if not isinstance(name, str):
        return "unknown"
    return {"codex": "codex", "codex-cli": "codex", "codex-mcp-client": "codex",
            "claude": "claude", "claude-code": "claude", "claude-ai": "claude",
            "kimi": "kimi", "kimi-code": "kimi", "kimiwork": "kimiwork",
            "kimi-work": "kimiwork"}.get(name, "unknown")


def identity_hash(domain: str, value: str) -> str:
    if not value:
        return ""
    key = os.environ.get("MC_TRACE_KEY") or config.MC_API_KEY
    key = key.encode("utf-8") if key else _PROCESS_KEY
    return hmac.new(key, ("mc-trace-v1\0" + domain + "\0" + value).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def hint_conflicts(raw_trace, hint) -> bool:
    """Resolve ambiguity before hashing; telemetry failures must not enable a hint."""
    _, hinted_trace, conflict = hint
    trace = parse_trace(raw_trace)
    return conflict or (trace is not None and hinted_trace is not None and trace != hinted_trace)


def attribution(raw_trace, client_session, hint, mcp_key: str, family: str) -> dict:
    from memory_compiler.freshness import valid_client_session
    trace = parse_trace(raw_trace)
    hinted_chat, hinted_trace, conflict = hint
    explicit_chat = valid_client_session(client_session)
    conflict = hint_conflicts(raw_trace, hint)
    # A supplied invalid trace stays unknown; never replace it with a hint.
    if raw_trace is MISSING and not conflict and (not explicit_chat or hinted_chat == client_session):
        trace = hinted_trace
    if explicit_chat:
        source, chat = "explicit", client_session
    elif conflict:
        source, chat = "conflict", ""
    elif hinted_chat:
        source, chat = "hint", hinted_chat
    else:
        source, chat = ("mcp", mcp_key) if mcp_key else ("unknown", "")
    return {"client_family": trace["family"] if trace else (family if raw_trace is MISSING else "unknown"),
            "correlation_id": trace.get("call_id", "") if trace else "",
            "device_hash": identity_hash("device", trace["device"]) if trace else "",
            "chat_hash": identity_hash("mcp" if source == "mcp" else "chat", chat),
            "chat_source": source}
