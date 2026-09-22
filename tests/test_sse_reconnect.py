"""/sse переживает переподключение потока (v1.90.1, багрепорт от 22.09.2026).

Клиенты legacy-SSE — Claude Code с "type": "sse" и mcp-remote --transport sse-only,
оба на TS SDK — при обрыве потока переподключаются САМИ. eventsource повторяет
GET /sse раз в 3 с, сервер заводит новую сессию и шлёт новый endpoint, транспорт
клиента молча подменяет session_id, а initialize заново НЕ шлёт. Строгая сессия
SDK отвечала на такой вызов -32602 «Invalid request parameters», причём до конца
жизни клиента: статус connected, штатный реконнект отказывает, потому что сервер
не «failed». Воспроизведено стендом тем же клиентским SDK; здесь тот же обмен
идёт через ASGI-приложение, без сети.

Клиента Starlette в проекте нет, поэтому поток читается прямо из ASGI send.
"""
import asyncio
import json
from pathlib import Path

from mcp.server import Server
from mcp.types import TextContent, Tool

from memory_compiler import api, tools
from memory_compiler.api import create_starlette_app

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
TIMEOUT = 5  # секунд на любой шаг обмена: зависание не должно вешать весь прогон

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "probe-client", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _call(request_id):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}}}


def _scope(method, path, query=b"", headers=()):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "root_path": "", "query_string": query,
            "headers": [(b"host", b"testserver"), *headers],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}


async def _post(app, endpoint, payload):
    """POST в /messages/ как клиент legacy-SSE; возвращает HTTP-статус."""
    path, _, query = endpoint.partition("?")
    body = json.dumps(payload).encode()
    sent, delivered = [], False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"application/json")]
    await asyncio.wait_for(app(_scope("POST", path, query.encode(), headers), receive, send), TIMEOUT)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


class SseStream:
    """Один GET /sse глазами клиента. Выход из контекста — обрыв потока."""

    def __init__(self, app):
        self._app = app
        self._events = asyncio.Queue()
        self._gone = asyncio.Event()
        self._buf = b""
        self._task: asyncio.Task | None = None
        self.endpoint = ""

    async def __aenter__(self):
        self._task = asyncio.create_task(self._app(_scope("GET", "/sse"), self._receive, self._send))
        event = await self._next_event()
        assert event.get("event") == "endpoint", event
        self.endpoint = event["data"]
        return self

    async def __aexit__(self, *_exc):
        self._gone.set()
        assert self._task is not None
        try:
            await asyncio.wait_for(self._task, TIMEOUT)
        except asyncio.TimeoutError:
            self._task.cancel()

    async def _receive(self):
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message):
        if message["type"] != "http.response.body":
            return
        self._buf += message.get("body", b"").replace(b"\r\n", b"\n")
        while b"\n\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n\n", 1)
            fields = dict(line.split(": ", 1) for line in raw.decode("utf-8").splitlines()
                          if ": " in line and not line.startswith(":"))
            if fields:
                await self._events.put(fields)

    async def _next_event(self):
        return await asyncio.wait_for(self._events.get(), TIMEOUT)

    async def notify(self, payload):
        """Уведомление в endpoint этого потока: ответа на него не бывает."""
        status = await _post(self._app, self.endpoint, payload)
        assert status == 202, status

    async def request(self, payload) -> dict:
        """Запрос в endpoint этого потока. В legacy-SSE POST лишь принимает
        сообщение (202), а ответ приходит событием потока."""
        await self.notify(payload)
        event = await self._next_event()
        assert event.get("event") == "message", event
        return json.loads(event["data"])


def _probe_server():
    """Сервер с одним инструментом: он называет клиента своей сессии."""
    server = Server("probe")

    @server.list_tools()
    async def _list_tools():
        return [Tool(name="whoami", description="Кто клиент этой сессии",
                     inputSchema={"type": "object", "properties": {}})]

    @server.call_tool()
    async def _call_tool(name, arguments):
        params = server.request_context.session.client_params
        who = params.clientInfo.name if params is not None else "без initialize"
        return [TextContent(type="text", text=who)]

    return server


def _starlette(monkeypatch, server):
    # Ключ из окружения разработчика превратил бы /sse в 401 — не наш предмет.
    monkeypatch.setattr(api, "MC_API_KEY", "")
    return create_starlette_app(server)


def test_call_after_silent_reconnect_is_served(monkeypatch):
    """Клиент инициализировался на первом потоке, поток оборвался, eventsource
    переподключился сам — вызов на новой сессии без initialize обязан пройти.

    Ломается, если handle_sse снова запустит сессию строго (stateless=False):
    вместо результата придёт -32602 «Invalid request parameters»."""
    app = _starlette(monkeypatch, _probe_server())

    async def scenario():
        async with SseStream(app) as first:
            assert "result" in await first.request(INIT)
            await first.notify(INITIALIZED)
            before = await first.request(_call(2))
        async with SseStream(app) as second:
            after = await second.request(_call(3))
        return first.endpoint, before, second.endpoint, after

    first_endpoint, before, second_endpoint, after = asyncio.run(scenario())

    assert first_endpoint != second_endpoint, "переподключение обязано дать новую сессию, иначе тест не про то"
    # Позитивный контроль: рукопожатие на первом потоке по-прежнему запоминает клиента.
    assert before["result"]["content"][0]["text"] == "probe-client", before
    assert "error" not in after, after
    assert after["result"]["content"][0]["text"] == "без initialize", after


def test_real_tool_call_after_silent_reconnect(monkeypatch):
    """Боевой путь: после переподключения вызов идёт через настоящий tools.call_tool
    на сессии без client_params. До v1.90.1 такие вызовы до call_tool не доходили
    вовсе, теперь доходят, поэтому всё, что читает client_params, обязано переносить
    None. Ломается и без stateless=True (-32602), и от чтения client_params без
    проверки: тогда SDK вернёт вызов с isError."""
    app = _starlette(monkeypatch, tools.app)

    async def scenario():
        async with SseStream(app) as first:
            assert "result" in await first.request(INIT)
            await first.notify(INITIALIZED)
        async with SseStream(app) as second:
            return await second.request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": "list_projects", "arguments": {}}})

    reply = asyncio.run(scenario())

    assert "error" not in reply, reply
    assert reply["result"].get("isError") is not True, reply
    assert "testproj" in reply["result"]["content"][0]["text"], reply


def test_server_info_carries_memory_compiler_version(monkeypatch):
    """В serverInfo — версия СЕРВЕРА. Без version= у Server SDK подставлял свою
    (pkg_version("mcp") = 1.29.1), и багрепорт назвал сервер «memory-compiler 1.29.1»."""
    app = _starlette(monkeypatch, tools.app)

    async def scenario():
        async with SseStream(app) as stream:
            return await stream.request(INIT)

    info = asyncio.run(scenario())["result"]["serverInfo"]

    assert info["name"] == "memory-compiler"
    assert info["version"] == VERSION_FILE.read_text(encoding="utf-8").strip()
