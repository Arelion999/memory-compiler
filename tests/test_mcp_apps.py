"""MCP Apps (io.modelcontextprotocol/ui): ресурс ui:// и ссылка на него из инструмента.

Контракт спеки 2026-01-26, ПРОВЕРЕН зондом v1.51.2 на живом клиенте:
  - ключ инструмента     `_meta.ui.resourceUri` (вложенный; плоский устарел);
  - mimeType ресурса     `text/html;profile=mcp-app`;
  - ui://-ресурсы МОЖНО не показывать в resources/list — хост берёт их по ссылке.

⚠️ Клиент объявляет принимаемый MIME на initialize: {"mimeTypes":
["text/html;profile=mcp-app"]}. Отдадим другой — хост ресурс не возьмёт, панель не
отрисуется, и это будет выглядеть как «клиент не поддерживает MCP Apps». В плане
работ был записан text/html+skybridge — это MIME OpenAI Apps SDK для ChatGPT, к
MCP Apps отношения не имеющий. Отсюда тест на точную строку.
"""
import asyncio

import pytest

from memory_compiler.tools import (
    UI_MIME,
    UI_SEARCH_RESOURCE,
    list_resources,
    list_tools,
    read_resource,
)
from memory_compiler.ui_app import PROTOCOL_VERSION, SEARCH_VIEW_HTML


def _tool(name):
    return next(t for t in asyncio.run(list_tools()) if t.name == name)


# ─── Ссылка инструмент → ресурс ──────────────────────────────────────────────

def test_search_tool_points_at_ui_resource():
    assert _tool("search").meta == {"ui": {"resourceUri": UI_SEARCH_RESOURCE}}


def test_meta_survives_serialization_under_its_alias():
    """Поле объявлено как `meta` с алиасом `_meta`, и на провод обязано уйти
    ПОД АЛИАСОМ. Мимо алиаса оно ставится МОЛЧА мимо цели: populate_by_name у
    Tool не выставлен, поэтому Tool(meta=...) не заполняет ничего, а хост потом
    просто не находит ссылку на вьюху."""
    dumped = _tool("search").model_dump(by_alias=True, exclude_none=True)
    assert dumped.get("_meta") == {"ui": {"resourceUri": UI_SEARCH_RESOURCE}}


def test_only_search_carries_ui_meta():
    """Ссылку несёт ровно один инструмент — иначе хост станет рисовать панель
    поиска в ответ на сохранение статьи."""
    with_meta = [t.name for t in asyncio.run(list_tools()) if (t.meta or {}).get("ui")]
    assert with_meta == ["search"]


# ─── Сам ресурс ──────────────────────────────────────────────────────────────

def test_ui_resource_is_served_with_the_mime_the_client_declared():
    got = asyncio.run(read_resource(UI_SEARCH_RESOURCE))
    assert got[0].mime_type == "text/html;profile=mcp-app" == UI_MIME


def test_ui_resource_returns_the_view_html():
    got = asyncio.run(read_resource(UI_SEARCH_RESOURCE))
    assert got[0].content == SEARCH_VIEW_HTML
    assert got[0].content.lstrip().startswith("<!DOCTYPE html>")


def test_ui_resource_absent_from_listing(knowledge_dir):
    """Спека разрешает не показывать ui:// в resources/list, и мы не показываем:
    листинг — это статьи базы, шаблон вьюхи там посторонний."""
    uris = [str(r.uri) for r in asyncio.run(list_resources())]
    assert not [u for u in uris if u.startswith("ui://")]


def test_memory_scheme_still_rejects_unknown_and_ui_does_not_leak_into_it():
    """ui:// и memory:// не должны протекать друг в друга."""
    bad = asyncio.run(read_resource("memory://memory-compiler/../../etc/passwd"))
    assert "❌" in bad[0].content or "не найдена" in bad[0].content
    unknown = asyncio.run(read_resource("ui://memory-compiler/нет-такой.html"))
    assert "❌" in unknown[0].content


# ─── Самодостаточность вьюхи (CSP хоста: default-src 'none') ─────────────────

@pytest.mark.parametrize("forbidden, why", [
    ("<script src=", "внешний скрипт не загрузится: script-src 'self'"),
    ("<link ", "внешняя таблица стилей не загрузится: style-src 'self'"),
    ("fetch(", "сеть закрыта целиком: connect-src 'none'"),
    ("XMLHttpRequest", "сеть закрыта целиком: connect-src 'none'"),
    ("/api/", "данные приходят от хоста, а не из нашего REST"),
    ("innerHTML", "заголовки статей — пользовательский контент, только textContent"),
])
def test_view_is_self_contained(forbidden, why):
    assert forbidden not in SEARCH_VIEW_HTML, why


def test_view_speaks_the_documented_handshake():
    """Хост не пришлёт НИЧЕГО до нотификации initialized — без неё панель молча
    останется пустой."""
    for method in ("ui/initialize", "ui/notifications/initialized",
                   "ui/notifications/tool-result"):
        assert method in SEARCH_VIEW_HTML, f"вьюха не знает метод {method}"


def test_view_reports_its_height():
    """Высотой панели управляет ОБМЕН, а не стили: хост обязан слушать
    `ui/notifications/size-changed` и подгонять iframe. Не слать размер — значит
    остаться в дефолтной высоте с прокруткой на полторы карточки (так и было
    в v1.52.0)."""
    assert "ui/notifications/size-changed" in SEARCH_VIEW_HTML
    assert "scrollHeight" in SEARCH_VIEW_HTML


def test_view_honours_container_dimensions():
    """`height` от хоста = размер фиксирован хостом, свой слать нельзя;
    `maxHeight` = потолок, выше которого просить бессмысленно."""
    assert "containerDimensions" in SEARCH_VIEW_HTML
    assert "maxHeight" in SEARCH_VIEW_HTML
    assert "fixedHeight" in SEARCH_VIEW_HTML


def test_size_is_measured_after_layout():
    """scrollHeight в том же кадре вернул бы высоту ПРЕДЫДУЩЕГО содержимого."""
    assert "requestAnimationFrame(sendSize)" in SEARCH_VIEW_HTML


def test_view_js_is_valid_syntax(tmp_path):
    """Синтаксическая ошибка во вьюхе = пустая панель БЕЗ единой жалобы: консоль
    песочного iframe нам не видна, сервер отдал ресурс успешно, тесты Python
    зелены. Требует node; без него пропускаем."""
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен — проверка синтаксиса JS вьюхи пропущена")

    js = re.search(r"<script>(.*?)</script>", SEARCH_VIEW_HTML, re.S)
    assert js, "во вьюхе не найден инлайновый <script> — рендерить будет нечем"
    f = tmp_path / "view.js"
    f.write_text(js.group(1), encoding="utf-8")
    done = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert done.returncode == 0, f"JS вьюхи невалиден:\n{done.stderr}"


def test_protocol_version_literal_matches_the_constant():
    """В HTML версия вписана литералом (подстановки нет намеренно) — сторож от
    тихого расхождения с константой модуля."""
    assert f'"{PROTOCOL_VERSION}"' in SEARCH_VIEW_HTML
