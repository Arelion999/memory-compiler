"""Real ASGI lifecycle: DELETE releases sessions; idle and stale paths still work."""
import asyncio
import json

from mcp.server import Server
from mcp.server.auth.provider import AccessToken
from mcp.server.streamable_http_manager import AuthenticatedUser

from memory_compiler import api
from tests.test_mcp_stale_session import INIT, INITIALIZED, _scope


USER = AuthenticatedUser(AccessToken(token="test", client_id="cleanup", scopes=[]))


async def _request(app, method, sid=None, payload=None, protocol="2025-06-18"):
    body = json.dumps(payload).encode() if payload is not None else b""
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    sent = []

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"application/json"),
               (b"accept", b"application/json, text/event-stream"),
               (b"mcp-protocol-version", protocol.encode())]
    if sid is not None:
        headers.append((b"mcp-session-id", sid.encode()))
    scope = _scope(method, "/mcp/", headers)
    scope["user"] = USER
    await asyncio.wait_for(app(scope, receive, send), 5)
    start = next(m for m in sent if m["type"] == "http.response.start")
    response_headers = {k.decode(): v.decode() for k, v in start["headers"]}
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], response_headers, raw


def _app(monkeypatch):
    monkeypatch.setattr(api, "MC_API_KEY", "")
    return api.create_starlette_app(Server("session-cleanup"))


async def _initialize(app):
    status, headers, body = await _request(app, "POST", payload=INIT)
    assert status == 200, body
    sid = headers["mcp-session-id"]
    status, _, body = await _request(app, "POST", sid, INITIALIZED)
    assert status == 202, body
    return sid


async def _ping(app, sid):
    status, _, body = await _request(app, "POST", sid,
                                    {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert status == 200, body
    assert b'"result":{}' in body, body


def test_delete_releases_transport_and_owner_repeatedly(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.router.lifespan_context(app):
            mgr = app.state.mcp_session_manager
            for _ in range(3):
                sid = await _initialize(app)
                assert sid in mgr._server_instances
                assert sid in mgr._session_owners
                status, _, body = await _request(app, "DELETE", sid)
                assert status == 200, body
                assert sid not in mgr._server_instances
                assert sid not in mgr._session_owners
                assert not mgr._server_instances
                assert not mgr._session_owners

    asyncio.run(scenario())


def test_delete_preserves_other_live_session_and_stale_policy(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.router.lifespan_context(app):
            mgr = app.state.mcp_session_manager
            closed = await _initialize(app)
            live = await _initialize(app)
            assert (await _request(app, "DELETE", closed))[0] == 200
            assert set(mgr._server_instances) == {live}
            assert set(mgr._session_owners) == {live}
            await _ping(app, live)
            # Unknown GET/DELETE stay strict; POST retains the restart fallback.
            assert (await _request(app, "GET", closed))[0] == 404
            assert (await _request(app, "DELETE", closed))[0] == 404
            await _ping(app, closed)
            assert set(mgr._server_instances) == {live}
            assert set(mgr._session_owners) == {live}

    asyncio.run(scenario())


def test_invalid_protocol_delete_does_not_remove_live_session(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.router.lifespan_context(app):
            mgr = app.state.mcp_session_manager
            sid = await _initialize(app)
            transport = mgr._server_instances[sid]
            status, _, body = await _request(app, "DELETE", sid, protocol="invalid")
            assert status == 400, body
            assert mgr._server_instances[sid] is transport
            assert not transport.is_terminated
            assert sid in mgr._session_owners
            await _ping(app, sid)

    asyncio.run(scenario())


def test_idle_timeout_really_releases_abandoned_session(monkeypatch):
    app = _app(monkeypatch)
    mgr = app.state.mcp_session_manager
    assert mgr.session_idle_timeout == 7200
    mgr.session_idle_timeout = 0.15

    async def scenario():
        async with app.router.lifespan_context(app):
            sid = await _initialize(app)
            assert sid in mgr._server_instances
            assert sid in mgr._session_owners
            async with asyncio.timeout(3):
                while sid in mgr._server_instances or sid in mgr._session_owners:
                    await asyncio.sleep(0.02)

    asyncio.run(scenario())
