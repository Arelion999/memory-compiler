"""Подсказки от хука (отпечаток вызова → id чата) — обход Kimi Code без updatedInput.

Аудит 28.09.2026: Kimi Code хукам updatedInput не отдаёт, служебный аргумент
_client_session до сервера не доезжает, и чаты склеиваются в одну MCP-сессию.
Хук PreToolUse шлёт на /api/session_hint пару (отпечаток вызова, id чата), а
call_tool подставляет id чата вызову с совпавшим отпечатком (считается по
СЫРЫМ args — до серверного heal).
"""

import asyncio
import json
import types

import pytest

from memory_compiler import api, freshness


@pytest.fixture(autouse=True)
def clean():
    freshness.reset()
    yield
    freshness.reset()


class FakeSession:
    """Объект-заглушка вместо MCP-сессии: важна только идентичность."""


def _bridge(monkeypatch):
    """Одна MCP-сессия на все чаты — как у моста Desktop."""
    from memory_compiler import handlers, tools

    class Ctx:
        session = FakeSession()

    class FakeApp:
        request_context = Ctx()

    monkeypatch.setattr(tools, "app", FakeApp())
    monkeypatch.setattr(handlers, "first_touch_context", lambda project: "")
    return tools


# ── freshness: хранилище подсказок ──────────────────────────────────────────

def test_hint_put_take_consume_on_use():
    freshness.hint_put("a" * 40, "chat-1")
    assert freshness.hint_take("a" * 40) == "chat-1"
    assert freshness.hint_take("a" * 40) == "", "подсказка не consume-on-use"
    assert freshness._hints == {}


def test_hint_take_unknown_and_malformed_fp():
    assert freshness.hint_take("b" * 40) == ""
    assert freshness.hint_take("not-a-fp") == ""
    assert freshness.hint_take("A" * 40) == "", "HEX в верхнем регистре не принимается"
    assert freshness._hints == {}


def test_hint_put_rejects_bad_input():
    for fp, cid in (("a" * 39, "chat-1"), ("a" * 41, "chat-1"), ("g" * 40, "chat-1"),
                    ("a" * 40, ""), ("a" * 40, "a b"), ("a" * 40, "x" * 200),
                    ("a" * 40, None), (None, "chat-1")):
        freshness.hint_put(fp, cid)
    assert freshness._hints == {}


def test_hint_expires_by_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(freshness, "time", types.SimpleNamespace(time=lambda: clock[0]))
    freshness.hint_put("a" * 40, "chat-1")
    clock[0] += freshness._HINT_TTL_SEC + 1
    assert freshness.hint_take("a" * 40) == ""
    assert freshness._hints == {}, "протухшая запись не удалена"


def test_hint_lazy_sweep_on_put(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(freshness, "time", types.SimpleNamespace(time=lambda: clock[0]))
    freshness.hint_put("a" * 40, "chat-1")
    freshness.hint_put("b" * 40, "chat-2")
    clock[0] += freshness._HINT_TTL_SEC + 1
    freshness.hint_put("c" * 40, "chat-3")     # чистка при put, не только при take
    assert list(freshness._hints) == ["c" * 40]


def test_hint_capped_oldest_dropped(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(freshness, "time", types.SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(freshness, "_HINT_MAX", 3)
    for i in range(5):
        clock[0] += 1
        freshness.hint_put("%040x" % i, "chat-%d" % i)
    assert len(freshness._hints) == 3
    assert "%040x" % 0 not in freshness._hints, "самая старая запись не вытеснена"
    assert freshness.hint_take("%040x" % 4) == "chat-4"


def test_reset_clears_hints():
    freshness.hint_put("a" * 40, "chat-1")
    freshness.reset()
    assert freshness._hints == {}


# ── call_fingerprint: паритет с дубликатом канонизации в хуке mc_guard.py ───
# Хук не может импортировать сервер и дублирует формулу — вектор ниже
# хардкодом ловит расхождение любой из сторон.

def test_call_fingerprint_parity_vector():
    fp = freshness.call_fingerprint("save_lesson", {"b": 1, "a": {"я": "тест", "z": [1, 2]}})
    assert fp == "0dab080e1acde7f350b12cae047758b895a460ee", \
        "канонизация разошлась с хуком mc_guard.py — обе стороны править вместе"


def test_call_fingerprint_ignores_key_order():
    a = freshness.call_fingerprint("save_lesson", {"b": 1, "a": {"я": "тест", "z": [1, 2]}})
    b = freshness.call_fingerprint("save_lesson", {"a": {"z": [1, 2], "я": "тест"}, "b": 1})
    assert a == b


# ── POST /api/session_hint ──────────────────────────────────────────────────

class JsonRequest:
    """Стенд под starlette.Request: эндпоинт читает json(), headers, cookies."""

    def __init__(self, payload=None, raw=None, auth=None):
        self._payload, self._raw = payload, raw
        self.headers = {"authorization": "Bearer " + auth} if auth else {}
        self.cookies = {}

    async def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._payload


def _call(req):
    resp = asyncio.run(api.web_session_hint(req))
    return resp.status_code, json.loads(resp.body)


def test_hint_endpoint_requires_auth(monkeypatch):
    body = {"fp": "a" * 40, "chat_id": "chat-1"}
    monkeypatch.setattr(api, "MC_API_KEY", "")
    assert _call(JsonRequest(body))[0] == 401
    monkeypatch.setattr(api, "MC_API_KEY", "secret-key")
    assert _call(JsonRequest(body))[0] == 401, "плохой Bearer не должен проходить"
    assert _call(JsonRequest(body, auth="secret-key"))[1] == {"ok": True}


def test_hint_endpoint_rejects_bad_input(monkeypatch):
    monkeypatch.setattr(api, "MC_API_KEY", "secret-key")
    assert _call(JsonRequest(raw="{не json", auth="secret-key"))[0] == 400
    assert _call(JsonRequest(["fp", "chat_id"], auth="secret-key"))[0] == 400
    assert _call(JsonRequest({"fp": "короткий", "chat_id": "chat-1"},
                             auth="secret-key"))[0] == 400
    assert _call(JsonRequest({"fp": "a" * 40, "chat_id": "a b"},
                             auth="secret-key"))[0] == 400
    assert freshness._hints == {}


def test_hint_endpoint_stores_hint(monkeypatch):
    monkeypatch.setattr(api, "MC_API_KEY", "secret-key")
    code, _ = _call(JsonRequest({"fp": "a" * 40, "chat_id": "chat-1"},
                                auth="secret-key"))
    assert code == 200
    assert freshness.hint_take("a" * 40) == "chat-1"


# ── интеграция диспетчера: подсказка подставляет id чата ────────────────────

def test_call_tool_takes_hint_for_fingerprint(monkeypatch, knowledge_dir):
    """Вызов без _client_session, но с подсказкой от хука: свежесть и ключ чата
    идут под c:<id> чата, а не под общим ключом MCP-сессии."""
    from mcp.types import TextContent
    (knowledge_dir / "infra").mkdir()
    tools = _bridge(monkeypatch)
    seen = []

    async def fake_dispatch(name, arguments):
        seen.append((freshness.chat_key_var.get(), dict(arguments)))
        return [TextContent(type="text", text="ok")]

    monkeypatch.setattr(tools, "_dispatch_tool", fake_dispatch)
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)

    args = {"project": "infra", "filename": "x.md"}
    fp = freshness.call_fingerprint("read_article", args)
    freshness.hint_put(fp, "chat-hint")

    asyncio.run(tools.call_tool("read_article", dict(args)))
    key, dispatched = seen[0]
    assert key == "c:chat-hint", "подсказка не подставила id чата"
    assert freshness.CLIENT_SESSION_ARG not in dispatched
    assert not freshness.is_first_touch(freshness.key_for(None, "chat-hint"), "infra"), \
        "свежесть не ушла под ключ чата"

    # consume-on-use: повторный вызов уже без подсказки — прежний ключ по сессии
    asyncio.run(tools.call_tool("read_article", dict(args)))
    assert seen[1][0] == "", "подсказка не consume-on-use: второй вызов унаследовал чат"
    assert freshness.key_for(tools.app.request_context.session).startswith("s")


def test_call_tool_without_hint_unchanged(monkeypatch, knowledge_dir):
    """Позитивный контроль: без подсказки поведение прежнее — ключ по MCP-сессии."""
    from mcp.types import TextContent
    (knowledge_dir / "infra").mkdir()
    tools = _bridge(monkeypatch)
    seen = []

    async def fake_dispatch(name, arguments):
        seen.append(freshness.chat_key_var.get())
        return [TextContent(type="text", text="ok")]

    monkeypatch.setattr(tools, "_dispatch_tool", fake_dispatch)
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)

    asyncio.run(tools.call_tool("read_article", {"project": "infra", "filename": "x.md"}))
    assert seen == [""]
