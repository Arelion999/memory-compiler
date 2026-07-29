"""MCP Apps (расширение io.modelcontextprotocol/ui): вьюха результатов поиска.

Хост забирает этот HTML по ссылке `_meta.ui.resourceUri` инструмента `search`,
рендерит в песочном iframe и общается с ним JSON-RPC поверх postMessage.

⚠️ НАМЕРЕННО ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ЧАСТЬ ui.py. В ui.py разметка собирается
подстановкой `str.replace` по ВСЕМУ документу, и на этом уже один раз положили
прод (см. предупреждение про плейсхолдер в ui.py и tests/test_ui_i18n.py). Здесь
подстановки нет вовсе: документ отдаётся как есть, все данные приходят в рантайме
сообщением от хоста. Стили похожи на ui.py по духу, но скопированы, а не
переиспользованы — связывать эти два файла механизмом нельзя.

⚠️ САМОДОСТАТОЧНОСТЬ ОБЯЗАТЕЛЬНА. CSP хоста по умолчанию (спека 2026-01-26):

    default-src 'none'; script-src 'self' 'unsafe-inline';
    style-src 'self' 'unsafe-inline'; img-src 'self' data:;
    media-src 'self' data:; connect-src 'none'

То есть инлайновые <script>/<style> разрешены, а ВНЕШНЕЕ НЕ ЗАГРУЗИТСЯ НИЧЕГО и
сеть закрыта целиком: `connect-src 'none'` запрещает fetch/XHR/WebSocket. Обращаться
к нашему же /api/* отсюда нельзя — и не нужно: данные приходят от хоста.

⚠️ ДАННЫЕ ВСТАВЛЯЮТСЯ ТОЛЬКО ЧЕРЕЗ textContent. Заголовки статей — пользовательский
контент из базы; innerHTML на них дал бы инъекцию в iframe. Ниже DOM строится
createElement'ом, innerHTML не используется нигде.
"""

# Протокол рукопожатия (спека 2026-01-26): вьюха шлёт запрос ui/initialize,
# дожидается результата, затем нотификацию ui/notifications/initialized. Хост НЕ
# ИМЕЕТ ПРАВА слать что-либо до неё — поэтому подписка на сообщения ставится до
# отправки рукопожатия, иначе первый же tool-result может прийти в пустоту.
PROTOCOL_VERSION = "2026-01-26"

SEARCH_VIEW_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Поиск по базе знаний</title>
<style>
  :root {
    --bg: transparent; --fg: #1f2328; --muted: #656d76;
    --card: #ffffff; --line: #d0d7de; --accent: #0969da;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --fg: #e6edf3; --muted: #8b949e;
      --card: #161b22; --line: #30363d; --accent: #4493f8;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 12px; background: var(--bg); color: var(--fg);
    font: 14px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; }
  .head b { font-size: 15px; }
  .head span { color: var(--muted); font-size: 13px; }
  ol { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  li {
    background: var(--card); border: 1px solid var(--line); border-radius: 6px;
    padding: 8px 10px;
  }
  .title { font-weight: 600; color: var(--accent); word-break: break-word; }
  .meta { color: var(--muted); font-size: 12px; margin-top: 2px; word-break: break-all; }
  .empty { color: var(--muted); padding: 8px 0; }
</style>
</head>
<body>
<div id="root"><div class="empty">Ожидание результатов…</div></div>
<script>
(function () {
  "use strict";
  var root = document.getElementById("root");
  var seq = 0, pending = {};

  function post(msg) { window.parent.postMessage(msg, "*"); }
  function notify(method, params) { post({ jsonrpc: "2.0", method: method, params: params || {} }); }
  function request(method, params) {
    var id = ++seq;
    return new Promise(function (resolve) {
      pending[id] = resolve;
      post({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
    });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function render(data) {
    var results = (data && data.results) || [];
    root.textContent = "";
    var head = el("div", "head");
    head.appendChild(el("b", null, data && data.query ? data.query : "Поиск"));
    head.appendChild(el("span", null, "найдено: " + ((data && data.count) || 0)));
    root.appendChild(head);
    if (!results.length) {
      root.appendChild(el("div", "empty", "Ничего не найдено."));
      return;
    }
    var list = el("ol");
    results.forEach(function (r) {
      var li = el("li");
      li.appendChild(el("div", "title", r.title || r.name || r.uri || ""));
      var meta = (r.name || "") + (r.score ? "  ·  " + r.score : "");
      if (meta.trim()) li.appendChild(el("div", "meta", meta));
      list.appendChild(li);
    });
    root.appendChild(list);
  }

  // Подписка ДО рукопожатия: хост шлёт tool-result сразу после initialized.
  window.addEventListener("message", function (e) {
    var m = e.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; return; }
    if (m.method === "ui/notifications/tool-result") {
      var p = m.params || {};
      render(p.structuredContent || {});
    } else if (m.method === "ui/notifications/tool-cancelled") {
      root.textContent = "";
      root.appendChild(el("div", "empty", "Запрос отменён."));
    }
  });

  request("ui/initialize", {
    protocolVersion: "2026-01-26",
    capabilities: {},
    clientInfo: { name: "memory-compiler-search-view", version: "1" }
  }).then(function () {
    notify("ui/notifications/initialized");
  });
})();
</script>
</body>
</html>
"""

# Версия протокола вписана в HTML ЛИТЕРАЛОМ, подстановки здесь нет намеренно:
# str.replace по всему документу — тот самый приём, которым в ui.py однажды залили
# многострочный CSS внутрь //-комментария и уронили весь скрипт на проде.
# Расхождение литерала с константой ловит tests/test_mcp_apps.py.
