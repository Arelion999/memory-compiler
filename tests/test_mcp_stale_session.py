"""/mcp переживает рестарт сервера: клиент со старым Mcp-Session-Id не получает 404.

Багрепорт 07.10.2026: контейнер рестартует по watcher'у, рестарт убивает серверные
сессии Streamable HTTP, а клиент Kimi Work держит кэшированный Mcp-Session-Id.
Строгий ответ SDK — 404 «Session not found» на любой запрос со старым id, включая
initialize; daimon помечает плагин disconnected и сам не возвращается (проверено:
~30 повторов за 2 ч, за ночь не восстановилось, лечится только перезапуском
приложения). StaleTolerantSessionManager обслуживает такой POST на одноразовом
транспорте, принимающем протухший id.

Стенд — тот же приём, что в test_sse_reconnect.py: обмен идёт через ASGI-приложение
без сети, «рестарт контейнера» — свежее create_starlette_app (пустой реестр сессий).
Клиента Starlette в проекте нет, поток читается прямо из ASGI send.
"""
import asyncio
import json

from mcp.server.streamable_http import MCP_SESSION_ID_HEADER

from memory_compiler import api, tools
from memory_compiler.api import create_starlette_app

TIMEOUT = 5  # секунд на любой шаг обмена: зависание не должно вешать весь прогон

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "probe-client", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _call(request_id):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}}}


def _scope(method, path, headers=()):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "root_path": "", "query_string": b"",
            "headers": [(b"host", b"testserver"), *headers],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}


async def _post_mcp(app, payload, sid=None):
    """POST в /mcp как streamable HTTP клиент: (status, headers, parsed_jsonrpc).

    Ответ инструмента в не-JSON режиме приходит SSE-стримом из одного
    `data:`-события — дожидаемся закрытия тела и парсим его."""
    body = json.dumps(payload).encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        # После тела запроса блокируемся: иначе sse-starlette получит
        # http.disconnect и отменит стрим ДО отправки тела ответа.
        await asyncio.Event().wait()

    sent = []

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"application/json"),
               (b"accept", b"application/json, text/event-stream")]
    if sid is not None:
        headers.append((MCP_SESSION_ID_HEADER.encode(), sid.encode()))
    await asyncio.wait_for(app(_scope("POST", "/mcp/", headers), receive, send), TIMEOUT)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    resp_headers = {k.decode(): v.decode() for m in sent if m["type"] == "http.response.start"
                    for k, v in m.get("headers", [])}
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    data_lines = [line[6:] for line in raw.replace(b"\r\n", b"\n").split(b"\n")
                  if line.startswith(b"data: ")]
    parsed = json.loads(data_lines[0]) if data_lines else None
    return status, resp_headers, parsed


def _starlette(monkeypatch):
    # Ключ из окружения разработчика превратил бы /mcp в 401 — не наш предмет.
    monkeypatch.setattr(api, "MC_API_KEY", "")
    return create_starlette_app(tools.app)


def test_stale_session_call_survives_server_restart(monkeypatch):
    """Клиент инициализировался, «контейнер рестартнулся» (новое приложение, пустой
    реестр сессий), tools/call со СТАРЫМ id обязан пройти, а не 404.

    Ломается на строгом StreamableHTTPSessionManager: 404 «Session not found» —
    именно тот ответ, после которого daimon помечает плагин disconnected навсегда."""
    app_before = _starlette(monkeypatch)
    app_after = _starlette(monkeypatch)

    async def scenario():
        async with app_before.router.lifespan_context(app_before):
            status, headers, reply = await _post_mcp(app_before, INIT)
            assert status == 200, (status, reply)
            sid = headers[MCP_SESSION_ID_HEADER.lower()]
            await _post_mcp(app_before, INITIALIZED, sid=sid)
            status, _, before = await _post_mcp(app_before, _call(2), sid=sid)
            assert status == 200 and "error" not in before, before
        # «Рестарт»: новое приложение, клиент НЕ переинициализируется (как Kimi Work)
        async with app_after.router.lifespan_context(app_after):
            status, headers, after = await _post_mcp(app_after, _call(3), sid=sid)
            return sid, status, headers, after

    sid, status, headers, after = asyncio.run(scenario())

    assert status != 404, ("404 Session not found — ровно тот ответ, что убивает "
                           "канал Kimi Work", after)
    assert "error" not in after, after
    assert after["result"].get("isError") is not True, after
    assert "testproj" in after["result"]["content"][0]["text"], after
    # Клиенту возвращается ЕГО id — он не замечает подмены транспорта и не лезет
    # в реинициализацию с тем же результатом 404.
    assert headers.get(MCP_SESSION_ID_HEADER.lower()) == sid, headers


def test_stale_session_initialize_also_served(monkeypatch):
    """Реинициализация со старым id (то, что daimon шлёт при reconnect-attempt)
    тоже обязана пройти: на строгом пути initialize+stale id — тот же 404."""
    app_after = _starlette(monkeypatch)

    async def scenario():
        async with app_after.router.lifespan_context(app_after):
            # «Старый» id — как будто выдан до рестарта, сервер его не знает
            return await _post_mcp(app_after, INIT, sid="deadbeef" * 8)

    status, headers, reply = asyncio.run(scenario())

    assert status == 200, (status, reply)
    assert "error" not in reply, reply
    assert reply["result"]["serverInfo"]["name"] == "memory-compiler", reply
    assert headers.get(MCP_SESSION_ID_HEADER.lower()) == "deadbeef" * 8, headers


def test_valid_session_still_stateful_and_registry_not_polluted(monkeypatch):
    """Регрессия: валидный id обслуживается штатным stateful-путём, а одноразовые
    транспорты протухших сессий не растят реестр (метрика mcp_sessions, idle-реап)."""
    app = _starlette(monkeypatch)

    async def scenario():
        async with app.router.lifespan_context(app):
            _, headers, _ = await _post_mcp(app, INIT)
            sid = headers[MCP_SESSION_ID_HEADER.lower()]
            await _post_mcp(app, INITIALIZED, sid=sid)
            valid, _, reply = await _post_mcp(app, _call(2), sid=sid)
            # три обращения с чужими id — каждое на одноразовом транспорте
            for i, _ in enumerate(range(3)):
                await _post_mcp(app, _call(10 + i), sid=f"stale-{i}")
            mgr = app.state.mcp_session_manager
            return sid, valid, reply, list(mgr._server_instances.keys())

    sid, valid, reply, registry = asyncio.run(scenario())

    assert valid == 200 and "error" not in reply, reply
    # в реестре ровно одна (валидная) stateful-сессия; протухшие не копятся
    assert registry == [sid], registry


def test_factory_uses_stale_tolerant_manager(monkeypatch):
    """Сторож подмены: фабрика обязана ставить толерантный менеджер — иначе правка
    молча откатилась к строгому SDK, и тесты выше проверяли бы не боевой путь."""
    monkeypatch.setattr(api, "MC_API_KEY", "")
    app = create_starlette_app(tools.app)
    mgr = app.state.mcp_session_manager
    assert type(mgr).__name__ == "StaleTolerantSessionManager", (
        "фабрика вернула строгий StreamableHTTPSessionManager — фикс не на боевом пути")
    assert isinstance(mgr, api.StreamableHTTPSessionManager), (
        "толерантный менеджер должен оставаться подклассом SDK — метрика mcp_sessions "
        "и idle-таймаут опираются на его реестр")
