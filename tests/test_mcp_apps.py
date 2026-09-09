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


def test_ui_resource_uri_carries_server_version():
    """Клиент забирает HTML вьюхи один раз и держит на всю MCP-сессию: рестарты
    контейнера сквозь mcp-remote кэш не сбрасывают (2026-09-09: прод отдавал
    новую вьюху, панель рисовала старую). Новая версия = новый URI. Старый URI
    из кэша и голый путь обязаны отвечать той же вьюхой, чужой путь — нет."""
    from memory_compiler import config
    from memory_compiler.tools import UI_SEARCH_PATH
    assert UI_SEARCH_RESOURCE == f"{UI_SEARCH_PATH}?v={config.VERSION}"
    for uri in (UI_SEARCH_PATH, f"{UI_SEARCH_PATH}?v=0.0.0", UI_SEARCH_RESOURCE):
        got = asyncio.run(read_resource(uri))
        assert got[0].content == SEARCH_VIEW_HTML, uri
    other = asyncio.run(read_resource("ui://memory-compiler/other.html?v=1"))
    assert "❌" in other[0].content


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


def test_view_opens_articles_through_read_article():
    """Смысл панели весь в этом: клик уходит на сервер НАПРЯМУЮ. В текстовой
    выдаче чтобы открыть статью нужен ход модели — генерация, токены, ожидание."""
    assert "tools/call" in SEARCH_VIEW_HTML
    assert "read_article" in SEARCH_VIEW_HTML


def test_view_marks_secrets_and_still_opens_them():
    """Секрет виден замком заранее и открывается кликом: read_article
    расшифровывает, а сам клик и есть осознанное раскрытие — тело не грузится,
    пока на карточку не нажали."""
    assert "🔒" in SEARCH_VIEW_HTML
    assert "r.secret" in SEARCH_VIEW_HTML


def test_view_filters_by_project_without_touching_the_server():
    assert 'el("button", "chip", p)' in SEARCH_VIEW_HTML
    assert "state.project" in SEARCH_VIEW_HTML


def test_cards_are_keyboard_reachable():
    """Карточка выглядит кликабельной — значит обязана быть достижимой с
    клавиатуры, иначе синий заголовок остаётся ложным обещанием."""
    assert 'setAttribute("tabindex", "0")' in SEARCH_VIEW_HTML
    assert 'setAttribute("role", "button")' in SEARCH_VIEW_HTML


def test_view_takes_theme_from_host_not_from_os():
    """Тема панели — от ХОСТА. prefers-color-scheme в песочном iframe отражает
    тему Windows, а не Claude Desktop: при тёмном чате и светлой ОС панель
    выходила светлой (и наоборот). Хост отдаёт theme в hostContext при
    ui/initialize и шлёт обновления в host-context-changed — оба пути обязаны
    вести в data-theme, а медиа-запрос оставаться лишь запасным."""
    assert "result.hostContext" in SEARCH_VIEW_HTML
    assert "ui/notifications/host-context-changed" in SEARCH_VIEW_HTML
    assert 'setAttribute("data-theme", ctx.theme)' in SEARCH_VIEW_HTML
    assert ':root[data-theme="dark"]' in SEARCH_VIEW_HTML
    # медиа-запрос не должен перебивать явную светлую тему хоста
    assert ':root:not([data-theme="light"])' in SEARCH_VIEW_HTML


def test_view_paints_with_host_palette_and_own_fallback():
    """Цвета — стандартные переменные --color-* из hostContext.styles.variables
    (спека 2026-01-26, Theming), свои значения только fallback: body без
    фона хоста прозрачен и показывает подложку iframe, которая после
    обновления Claude Desktop 09.09.2026 стала светлее чата."""
    for var in ("--color-background-primary", "--color-background-secondary",
                "--color-text-primary", "--color-text-secondary",
                "--color-border-primary", "--font-sans"):
        assert f"var({var}," in SEARCH_VIEW_HTML, f"вьюха не берёт {var} у хоста"
    assert "doc.style.setProperty(k, vars[k])" in SEARCH_VIEW_HTML
    # чужие ключи в переменные не льём: только начинающиеся с --
    assert 'k.indexOf("--") === 0' in SEARCH_VIEW_HTML


def test_view_shows_host_diagnostics_collapsed():
    """Единственный канал из песочного iframe наружу — сама панель: консоли нет,
    сети нет. Свёрнутый блок показывает, что реально прислал хост (замер
    2026-09-09: НИЧЕГО — ни hostContext, ни theme, ни variables). Раскрытым по
    умолчанию быть не должен — это отладка, а не выдача."""
    assert "диагностика хоста" in SEARCH_VIEW_HTML
    assert "hostDiag" in SEARCH_VIEW_HTML
    assert "d.open = true" not in SEARCH_VIEW_HTML


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


# ─── Панель обязана читать всё, что сервер ей кладёт (v1.72.1) ───────────────
# Аудит 27.08.2026 по классу «поле передано — потребитель не взял». Сервер
# дублирует футеры (свежесть, подсказка первого обращения, напоминание о
# session_note) в structuredContent полем `notice` — именно потому, что у search
# объявлен outputSchema и дополнительный TextContent до модели не доходит
# (v1.68.0). А вьюха читала results/query/count и поля результата, но `notice`
# не читала ВОВСЕ: модель подсказку получала, человек в панели — нет, и
# ни ошибки, ни пустого места, просто тишина.
#
# ⚠️ Сторож НЕ на одно поле, а на ВЕСЬ контракт: любое новое поле outputSchema
# обязано быть прочитано вьюхой, иначе это повторение того же класса на новом
# месте — а заметить его снова будет нечем.

def _search_output_schema():
    return _tool("search").outputSchema


def test_view_reads_every_top_level_field_the_server_sends():
    props = _search_output_schema()["properties"]
    unread = [k for k in props if ("data.%s" % k) not in SEARCH_VIEW_HTML]
    assert not unread, (
        "сервер кладёт в structuredContent поля, которых панель не читает: %s" % unread)


def test_view_reads_every_result_field_the_server_sends():
    item = _search_output_schema()["properties"]["results"]["items"]["properties"]
    unread = [k for k in item if ("r.%s" % k) not in SEARCH_VIEW_HTML]
    assert not unread, "панель не читает поля результата: %s" % unread


def test_notice_is_rendered_as_text_not_markup():
    """Текст подсказки собирает сервер, но правило то же, что для заголовков:
    в DOM он попадает текстом. innerHTML во вьюхе запрещён отдельным тестом —
    здесь проверяем, что подсказка вообще доходит до отрисовки."""
    assert "data.notice" in SEARCH_VIEW_HTML
    assert "notice" in SEARCH_VIEW_HTML


# ─── Диагностика показывается, только когда хосту есть что сказать (v1.75.1) ──
# Замер 2026-09-09 (Claude Desktop 1.49585): hostContext при initialize НЕТ,
# theme не прислан, styles.variables нет, containerDimensions нет. Блок из
# v1.74.3 при этом всё равно рисовался и сообщал «НЕТ / не прислан / нет» —
# место занимала ровно та ситуация, где информации ноль.
#
# ⚠️ УДАЛИТЬ БЛОК СОВСЕМ БЫЛО БЫ ДОРОЖЕ. Проверка панели стоит полного
# перезапуска приложения: клиент кэширует HTML вьюхи на всю свою жизнь
# (v1.74.3), и без зонда смена поведения хоста заметится случайно. Поэтому
# условие ПЕРЕВЁРНУТО: пусто — блока нет, хост заговорил — блок появляется сам
# и служит сигналом, что можно включать поддержку темы из v1.74.2.


def _extract_js_function(name):
    """Тело функции верхнего уровня из вьюхи — по балансу фигурных скобок.

    Тот же приём, что в tests/test_graph_worker.py: JS живёт внутри строки
    Python, и разбирать его иначе нечем.
    """
    start = SEARCH_VIEW_HTML.index("function %s(" % name)
    depth, i = 0, SEARCH_VIEW_HTML.index("{", start)
    while True:
        ch = SEARCH_VIEW_HTML[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SEARCH_VIEW_HTML[start:i + 1]
        i += 1


def _run_predicate(diag_js):
    """Прогнать hostSpoke в node с заданным аргументом — проверяем ПОВЕДЕНИЕ."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node недоступен")
    js = _extract_js_function("hostSpoke") + "\nconsole.log(JSON.stringify(!!hostSpoke(%s)));" % diag_js
    done = subprocess.run([node, "-e", js], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip())


def test_diag_is_hidden_when_host_said_nothing():
    assert _run_predicate("{init: null, changes: 0, vars: {}}") is False


def test_diag_appears_when_host_sends_context():
    assert _run_predicate("{init: {theme: 'dark'}, changes: 0, vars: {}}") is True


def test_diag_appears_after_host_context_changed():
    assert _run_predicate("{init: null, changes: 1, vars: {}}") is True


def test_diag_appears_when_variables_arrive():
    assert _run_predicate("{init: null, changes: 0, vars: {'--color-text-primary': '#fff'}}") is True


def test_predicate_survives_missing_argument():
    """Панель не должна падать, если объекта диагностики ещё нет."""
    assert _run_predicate("undefined") is False


def test_view_calls_the_predicate_before_rendering_diag():
    """Позитивный контроль: блок рисуется ПОД условием, а не всегда."""
    assert "hostSpoke(hostDiag)" in SEARCH_VIEW_HTML
    assert "root.appendChild(renderDiag())" in SEARCH_VIEW_HTML
    idx = SEARCH_VIEW_HTML.index("root.appendChild(renderDiag())")
    line_start = SEARCH_VIEW_HTML.rfind(chr(10), 0, idx)
    assert "if (" in SEARCH_VIEW_HTML[line_start:idx], "вызов обязан стоять под условием"
