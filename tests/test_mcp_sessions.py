"""Число живых MCP-сессий в /api/health — чтобы сессии-сироты стало чем мерить.

У Streamable HTTP (`/mcp`) сессии stateful. Дефолт SDK `session_idle_timeout=None`
означал бы, что простаивающая сессия не истекает вообще и сироты копятся до
рестарта; с v1.86.1 фабрика передаёт SDK `MCP_SESSION_IDLE_TIMEOUT` (2 ч), и SDK
реапит брошенные сам. Метрика осталась нужной: по ней видно, что реестр не растёт
монотонно в тихие периоды без рестартов. Мерить приходится из реестра менеджера,
потому что SDK сообщает о рождении транспорта строкой `Created new transport with
session ID` на уровне INFO, а логгер `mcp` у нас намеренно держится на WARNING
(obs.py) — там рождаются транспортные `-32602`/`-32001`, и поднятие зальёт лог шумом.
"""
import asyncio
import json

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from memory_compiler.api import create_starlette_app, web_health


class Req:
    """Стенд под starlette.Request: health читает headers/cookies и app.state.

    Клиента Starlette в проекте нет — эндпоинты зовутся напрямую, как в
    test_web_security.py и test_probe_api.py."""

    def __init__(self, headers=None, cookies=None, app=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        if app is not None:
            self.app = app


class _State:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _App:
    def __init__(self, **state):
        self.state = _State(**state)


def _health(request=None):
    return json.loads(asyncio.run(web_health(request or Req())).body)


def test_health_exposes_mcp_sessions_publicly(knowledge_dir):
    """Поле публичное — по тем же основаниям, что models_ready и embed_pending.

    Это состояние сервера, а не сведения о содержимом базы: ни имён проектов,
    ни статей число сессий не раскрывает, зато его видит и Docker healthcheck,
    и человек, у которого «сессии копятся»."""
    body = _health()
    assert "mcp_sessions" in body, body
    assert body["status"] == "ok" and "version" in body, "поле сломало публичный ответ"


def test_health_counts_live_sessions_from_real_manager(knowledge_dir):
    """Позитивный контроль: два живых транспорта → 2.

    Без него «ключ есть» проходило бы и на заглушке, вечно отдающей ноль, — а
    ноль и есть тот ответ, ради опровержения которого метрика заводится.
    Реестр берётся у НАСТОЯЩЕГО менеджера SDK: тест обязан ловить и переименование
    приватного поля, а не только нашу арифметику."""
    mgr = StreamableHTTPSessionManager(app=Server("probe"))
    mgr._server_instances["sess-a"] = object()
    mgr._server_instances["sess-b"] = object()
    body = _health(Req(app=_App(mcp_session_manager=mgr)))
    assert body["mcp_sessions"] == 2, body


def test_health_reports_none_when_registry_is_gone(knowledge_dir):
    """«Мерить нечем» обязано отличаться от «сессий ноль».

    Отдай мы 0 — исчезнувшее поле SDK выглядело бы как здоровый сервер без
    сессий, и вопрос про сирот получил бы ложный отрицательный ответ."""
    class NoRegistry:
        pass

    class WrongType:
        _server_instances = ["не словарь"]

    for manager in (NoRegistry(), WrongType()):
        body = _health(Req(app=_App(mcp_session_manager=manager)))
        assert body["mcp_sessions"] is None, (manager, body)
        assert body["status"] == "ok", "health не должен падать из-за метрики"


def test_health_survives_request_without_app(knowledge_dir):
    """Прямой вызов эндпоинта (тесты, внутренние пробы) идёт без request.app."""
    body = _health(Req())
    assert body["mcp_sessions"] is None, body


def test_app_state_carries_session_manager():
    """Менеджер доезжает до эндпоинта: фабрика кладёт его в state приложения.

    Раньше это была локальная переменная create_starlette_app, и web_health до
    неё не дотягивался — метрику неоткуда было взять."""
    app = create_starlette_app(Server("test"))
    mgr = getattr(app.state, "mcp_session_manager", None)
    assert isinstance(mgr, StreamableHTTPSessionManager), (
        "менеджер Streamable HTTP не выставлен в app.state — /api/health отдаст "
        "mcp_sessions=None на живом сервере")


def test_session_manager_gets_idle_timeout():
    """С v1.86.1 простаивающие сессии истекают: фабрика ОБЯЗАНА передать SDK
    session_idle_timeout, иначе сироты снова копятся до рестарта (дефолт SDK — None,
    «не истекают вообще»). Проверяем, что значение доехало до менеджера SDK, а не
    просто объявлено константой — иначе правка молча откатилась бы к дефолту."""
    from memory_compiler.api import MCP_SESSION_IDLE_TIMEOUT
    assert MCP_SESSION_IDLE_TIMEOUT == 7200, "порог простоя изменился — обнови и это ожидание"
    app = create_starlette_app(Server("test"))
    mgr = app.state.mcp_session_manager
    assert getattr(mgr, "session_idle_timeout", None) == MCP_SESSION_IDLE_TIMEOUT, (
        "session_idle_timeout не доехал до менеджера SDK — простаивающие сессии "
        "снова не истекают (дефолт None), реестр растёт до рестарта")


def test_sdk_still_exposes_private_session_registry():
    """СТОРОЖ ПРИВАТНОЙ ОПОРЫ (родня test_client_capabilities.py).

    Публичных атрибутов у StreamableHTTPSessionManager два — handle_request и run;
    реестра живых сессий среди них нет. Считаем по `_server_instances`, то есть по
    приватному полю чужой библиотеки. Переименуют или уберут — метрика МОЛЧА станет
    `None`, и «сессий нет» будет неотличимо от «мерить перестали»; падать должно
    здесь, а не через месяц, когда по пустой метрике закроют вопрос про сирот."""
    mgr = StreamableHTTPSessionManager(app=Server("probe"))
    reg = getattr(mgr, "_server_instances", None)
    assert isinstance(reg, dict), (
        "SDK больше не держит реестр сессий в StreamableHTTPSessionManager."
        "_server_instances — метрика mcp_sessions теперь всегда None, и «сессий "
        "нет» неотличимо от «мерить перестали». Найти новое имя реестра ДО того, "
        "как верить нулю в /api/health."
    )
