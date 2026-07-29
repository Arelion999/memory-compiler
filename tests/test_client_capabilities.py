"""Зонд поддержки MCP Apps: что клиент объявляет на initialize.

Расширение `io.modelcontextprotocol/ui` (спека 2026-01-26) клиент объявляет сам:

    capabilities.extensions["io.modelcontextprotocol/ui"] = {"mimeTypes": [...]}

Это машиночитаемый ответ на вопрос «умеет ли хост MCP Apps» — точнее, чем смотреть
глазами, отрисовалась ли панель в чате.

⚠️ ЧЕМ ЭТО ДЕРЖИТСЯ И ПОЧЕМУ ЗДЕСЬ ТЕСТ. `ClientCapabilities` нашего пина 1.28.1
поля `extensions` НЕ МОДЕЛИРУЕТ (в нём ровно experimental/sampling/elicitation/
roots/tasks). Читается оно только потому, что у модели `model_config extra="allow"`,
то есть незнакомое поле переживает валидацию и лежит в дампе как есть. Это
недокументированная опора на поведение чужой библиотеки: стоит ей смениться на
extra="ignore", и зонд молча начнёт возвращать «клиент не поддерживает UI» —
неотличимо от честного отрицательного ответа. Отсюда отдельный тест на сам факт
выживания поля: он должен упасть на бампе SDK, а не после того, как мы на основании
пустого зонда закроем целое направление роадмапа.
"""
import mcp.types as types

from memory_compiler.tools import UI_EXTENSION, client_ui_support


def _params(extensions: dict | None) -> types.InitializeRequestParams:
    caps: dict = {}
    if extensions is not None:
        caps["extensions"] = extensions
    return types.InitializeRequestParams.model_validate({
        "protocolVersion": "2026-01-26",
        "clientInfo": {"name": "probe-client", "version": "9.9.9"},
        "capabilities": caps,
    })


def test_unknown_capability_field_survives_sdk_validation():
    """Опора зонда: SDK не выбрасывает не смоделированное им `extensions`.

    Падение здесь означает не «клиент не поддерживает UI», а «зонд ослеп».
    """
    params = _params({UI_EXTENSION: {"mimeTypes": ["text/html;profile=mcp-app"]}})
    dumped = params.capabilities.model_dump(by_alias=True, exclude_none=True)
    assert "extensions" in dumped, (
        "SDK перестал сохранять незнакомые поля capabilities — зонд MCP Apps "
        "теперь всегда отвечает «не поддерживается». Проверить model_config "
        "у ClientCapabilities перед тем, как верить отрицательному результату."
    )


def test_declared_ui_extension_is_detected():
    info = client_ui_support(_params({UI_EXTENSION: {"mimeTypes": ["text/html;profile=mcp-app"]}}))
    assert info["ui_extension"] == {"mimeTypes": ["text/html;profile=mcp-app"]}
    assert info["client"] == "probe-client"
    assert info["version"] == "9.9.9"


def test_client_without_ui_extension_reports_none_but_lists_others():
    """Отрицательный ответ обязан быть отличим от «ничего не объявлено вообще»."""
    info = client_ui_support(_params({"io.example/other": {}}))
    assert info["ui_extension"] is None
    assert info["extensions"] == ["io.example/other"]


def test_client_without_any_extensions():
    info = client_ui_support(_params(None))
    assert info["ui_extension"] is None
    assert info["extensions"] == []


def test_missing_client_params_does_not_crash():
    """Вне запроса (прямой вызов диспетчера, тесты) зонд обязан молчать, а не падать."""
    info = client_ui_support(None)
    assert info["ui_extension"] is None
    assert info["client"] == "?"
