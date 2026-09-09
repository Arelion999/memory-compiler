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

# СТРОКА RAW (r"""), и это не украшение. Внутри лежит JS, а в JS backslash —
# рабочий символ: обычная строка съела бы `\n` из parts.join("\n") как настоящий
# перевод строки, разорвав литерал прямо посреди кода. Поймано node --check'ом
# из tests/test_mcp_apps.py — глазами такое не видно, а панель молча остаётся
# пустой: консоль песочного iframe нам недоступна.
SEARCH_VIEW_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Поиск по базе знаний</title>
<style>
  /* Тема — ОТ ХОСТА, а не от ОС. prefers-color-scheme в песочном iframe
     отражает тему Windows, а не тему приложения; хост присылает свою в
     hostContext.theme, скрипт ставит её в data-theme. Медиа-запрос остаётся
     запасным путём на случай, если хост темы не прислал. Цвета — тоже от
     хоста: стандартные переменные --color-* из hostContext.styles.variables
     (спека 2026-01-26, раздел Theming), свои — только fallback. */
  :root {
    --fb-bg: transparent; --fb-fg: #1f2328; --fb-muted: #656d76;
    --fb-card: #ffffff; --fb-line: #d0d7de; --fb-accent: #0969da;
  }
  :root[data-theme="dark"] {
    --fb-fg: #e6edf3; --fb-muted: #8b949e;
    --fb-card: #161b22; --fb-line: #30363d; --fb-accent: #4493f8;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --fb-fg: #e6edf3; --fb-muted: #8b949e;
      --fb-card: #161b22; --fb-line: #30363d; --fb-accent: #4493f8;
    }
  }
  :root {
    --bg: var(--color-background-primary, var(--fb-bg));
    --fg: var(--color-text-primary, var(--fb-fg));
    --muted: var(--color-text-secondary, var(--fb-muted));
    --card: var(--color-background-secondary, var(--fb-card));
    --line: var(--color-border-primary, var(--fb-line));
    --accent: var(--color-text-info, var(--fb-accent));
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 12px; background: var(--bg); color: var(--fg);
    font: 14px/1.5 var(--font-sans, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);
  }
  .head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
  .head b { font-size: 15px; }
  .head span { color: var(--muted); font-size: 13px; }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; }
  .chip {
    font: inherit; font-size: 12px; cursor: pointer; color: var(--muted);
    background: var(--card); border: 1px solid var(--line);
    border-radius: 999px; padding: 2px 10px;
  }
  .chip[aria-pressed="true"] { color: var(--accent); border-color: var(--accent); }
  ol { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  li {
    background: var(--card); border: 1px solid var(--line); border-radius: 6px;
    padding: 8px 10px; cursor: pointer;
  }
  li:hover, li:focus { border-color: var(--accent); outline: none; }
  .title { font-weight: 600; color: var(--accent); word-break: break-word; }
  .meta { color: var(--muted); font-size: 12px; margin-top: 2px; word-break: break-all; }
  .empty { color: var(--muted); padding: 8px 0; }
  .notice {
    background: var(--card); border-left: 3px solid var(--accent);
    border-radius: 4px; color: var(--muted); font-size: 12px; line-height: 1.5;
    padding: 8px 10px; margin-bottom: 10px;
    white-space: pre-wrap; word-break: break-word;
  }
  .diag { margin-top: 10px; font-size: 12px; color: var(--muted); }
  .diag summary { cursor: pointer; }
  .back {
    font: inherit; font-size: 13px; cursor: pointer; color: var(--accent);
    background: none; border: none; padding: 0; margin-bottom: 8px;
  }
  .article {
    background: var(--card); border: 1px solid var(--line); border-radius: 6px;
    padding: 10px 12px; margin: 0; white-space: pre-wrap; word-break: break-word;
    font: 13px/1.55 var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  }
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

  // Высота НЕ настраивается стилями: ею управляет обмен. Хост присылает
  // containerDimensions (height = фиксированная, хостовая; maxHeight = гибкая,
  // нашa, до потолка; ничего = без ограничений), и обязан слушать
  // ui/notifications/size-changed. Не прислать размер = остаться в дефолтной
  // высоте с полосой прокрутки на полторы карточки.
  var maxHeight = null, fixedHeight = false;

  // Тема и палитра хоста. Без этого панель красится по теме ОС: в песочном
  // iframe prefers-color-scheme берётся из Windows, а тема Claude Desktop может
  // быть другой — и панель выходит светлой в тёмном чате (и наоборот). Хост
  // отдаёт theme и styles.variables в hostContext при ui/initialize и шлёт
  // частичные обновления в ui/notifications/host-context-changed (переключили
  // тему — панель обязана перекраситься, не дожидаясь нового поиска).
  // Диагностика: что реально прислал хост. Из песочного iframe консоли нет, сети
  // нет, поэтому единственный канал наружу — сама панель. Блок свёрнут.
  var hostDiag = { init: null, changes: 0, vars: {} };

  function applyHost(ctx) {
    if (!ctx || typeof ctx !== "object") return;
    if (hostDiag.init === null) hostDiag.init = ctx; else hostDiag.changes++;
    var doc = document.documentElement;
    if (ctx.theme === "light" || ctx.theme === "dark") {
      doc.setAttribute("data-theme", ctx.theme);
      doc.style.colorScheme = ctx.theme;
    }
    var st = ctx.styles || {};
    var vars = st.variables || {};
    for (var k in vars) {
      if (typeof vars[k] === "string" && k.indexOf("--") === 0) { doc.style.setProperty(k, vars[k]); hostDiag.vars[k] = vars[k]; }
    }
    var fonts = st.css && st.css.fonts;
    if (typeof fonts === "string" && fonts && !document.getElementById("host-fonts")) {
      var tag = document.createElement("style");
      tag.id = "host-fonts";
      tag.textContent = fonts;
      document.head.appendChild(tag);
    }
  }

  function findDims(o, depth) {
    // containerDimensions живёт в HostContext результата ui/initialize. Точный
    // путь в спеке не закреплён, поэтому ищем по имени — промах по пути молча
    // вернул бы «размер не сообщаем», то есть ровно исходный дефект.
    if (!o || typeof o !== "object" || depth > 4) return null;
    if (o.containerDimensions) return o.containerDimensions;
    for (var k in o) {
      var f = findDims(o[k], depth + 1);
      if (f) return f;
    }
    return null;
  }

  function sendSize() {
    if (fixedHeight) return;          // высотой распоряжается хост — молчим
    var d = document.documentElement;
    var h = Math.ceil(d.scrollHeight);
    if (maxHeight) h = Math.min(h, maxHeight);
    notify("ui/notifications/size-changed", { width: Math.ceil(d.scrollWidth), height: h });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  var state = { data: null, project: null };

  function openArticle(r) {
    root.textContent = "";
    var back = el("button", "back", "← к результатам");
    back.addEventListener("click", function () { renderList(); });
    root.appendChild(back);
    root.appendChild(el("div", "title", r.title || r.name || ""));
    var body = el("pre", "article", "Загрузка…");
    root.appendChild(body);
    requestAnimationFrame(sendSize);
    // Идём к серверу НАПРЯМУЮ: модель в этом не участвует — ни хода, ни токенов.
    // Секрет открывается тем же путём: read_article расшифровывает, а сам клик и
    // есть осознанное раскрытие (тело не грузится, пока на карточку не нажали).
    request("tools/call", { name: "read_article", arguments: { project: r.project, filename: r.file } })
      .then(function (res) {
        var parts = ((res && res.content) || [])
          .filter(function (c) { return c && c.type === "text"; })
          .map(function (c) { return c.text; });
        body.textContent = parts.length ? parts.join("\n") : "Пусто.";
        requestAnimationFrame(sendSize);
      });
  }

  function renderList() {
    var data = state.data || {};
    var all = data.results || [];
    var shown = state.project ? all.filter(function (r) { return r.project === state.project; }) : all;
    root.textContent = "";

    var head = el("div", "head");
    head.appendChild(el("b", null, data.query || "Поиск"));
    head.appendChild(el("span", null, "найдено: " + (data.count || 0)));
    root.appendChild(head);

    // Подсказки сервера (свежесть от параллельных сессий, первое обращение к
    // проекту, напоминание о заметке) приезжают полем notice: у search объявлен
    // outputSchema, и отдельный текстовый блок до клиента не доходит — потому
    // они и продублированы в structuredContent. Не показать их здесь значило бы
    // отдать подсказку модели и спрятать от человека, молча.
    // Текст многострочный, поэтому white-space: pre-wrap; в DOM — только
    // textContent, как и заголовки статей.
    if (data.notice) root.appendChild(el("div", "notice", data.notice));

    // Фильтр по проекту — поверх УЖЕ полученных результатов, без обращения к
    // серверу и без хода модели.
    var projects = [];
    all.forEach(function (r) { if (r.project && projects.indexOf(r.project) < 0) projects.push(r.project); });
    if (projects.length > 1) {
      var chips = el("div", "chips");
      projects.forEach(function (p) {
        var b = el("button", "chip", p);
        b.setAttribute("aria-pressed", state.project === p ? "true" : "false");
        b.addEventListener("click", function () {
          state.project = state.project === p ? null : p;
          renderList();
        });
        chips.appendChild(b);
      });
      root.appendChild(chips);
    }

    if (!shown.length) {
      root.appendChild(el("div", "empty", "Ничего не найдено."));
    } else {
      var list = el("ol");
      shown.forEach(function (r) {
        var li = el("li");
        li.setAttribute("role", "button");
        li.setAttribute("tabindex", "0");
        li.appendChild(el("div", "title", (r.secret ? "🔒 " : "") + (r.title || r.name || r.uri || "")));
        var meta = (r.name || "") + (r.score ? "  ·  " + r.score : "");
        if (r.secret) meta += "  ·  зашифровано, открыть — по клику";
        if (meta.trim()) li.appendChild(el("div", "meta", meta));
        li.addEventListener("click", function () { openArticle(r); });
        li.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openArticle(r); }
        });
        list.appendChild(li);
      });
      root.appendChild(list);
    }
    root.appendChild(renderDiag());
    // После вёрстки, а не в этом кадре: scrollHeight до перерасчёта layout'а
    // вернул бы высоту ПРЕДЫДУЩЕГО содержимого.
    requestAnimationFrame(sendSize);
  }

  // Замер 2026-09-09 (Claude Desktop 1.49585): hostContext при initialize НЕТ,
  // theme не прислан, styles.variables нет, containerDimensions нет. То есть
  // всё, что панель умеет брать у хоста, пока спит; цвета — свои, по
  // prefers-color-scheme. Блок оставлен свёрнутым внизу: когда хост начнёт
  // что-то присылать, это будет видно без новой отладочной сборки.
  function renderDiag() {
    var d = el("details", "diag");
    d.appendChild(el("summary", null, "диагностика хоста"));
    var lines = [];
    var init = hostDiag.init;
    lines.push("hostContext при initialize: " + (init ? "есть, ключи: " + Object.keys(init).join(", ") : "НЕТ"));
    lines.push("theme от хоста: " + (init && init.theme ? init.theme : "не прислан"));
    lines.push("host-context-changed получено: " + hostDiag.changes);
    var vk = Object.keys(hostDiag.vars);
    lines.push("styles.variables: " + (vk.length ? vk.length + " шт." : "нет"));
    ["--color-background-primary", "--color-background-secondary", "--color-text-primary", "--font-sans"].forEach(function (k) {
      if (hostDiag.vars[k] !== undefined) lines.push("  " + k + " = " + hostDiag.vars[k]);
    });
    if (vk.length) lines.push("  все ключи: " + vk.join(" "));
    var mq = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    lines.push("prefers-color-scheme: " + (mq && mq.matches ? "dark" : "light"));
    var cs = getComputedStyle(document.documentElement);
    lines.push("data-theme: " + (document.documentElement.getAttribute("data-theme") || "нет") +
      ", color-scheme: " + (cs.colorScheme || "?"));
    lines.push("фон body (computed): " + getComputedStyle(document.body).backgroundColor);
    lines.push("containerDimensions: " + (init && findDims(init, 0) ? JSON.stringify(findDims(init, 0)) : "нет") +
      ", fixedHeight=" + fixedHeight + ", maxHeight=" + maxHeight);
    lines.push("размер документа: " + document.documentElement.scrollWidth + "×" + document.documentElement.scrollHeight);
    d.appendChild(el("pre", "article", lines.join("\n")));
    d.addEventListener("toggle", function () { requestAnimationFrame(sendSize); });
    return d;
  }

  function render(data) {
    state.data = data || {};
    state.project = null;
    renderList();
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
    } else if (m.method === "ui/notifications/host-context-changed") {
      applyHost(m.params);
    }
  });

  request("ui/initialize", {
    protocolVersion: "2026-01-26",
    capabilities: {},
    clientInfo: { name: "memory-compiler-search-view", version: "1" }
  }).then(function (result) {
    applyHost(result && result.hostContext);
    var dims = findDims(result, 0);
    if (dims) {
      fixedHeight = dims.height !== undefined && dims.height !== null;
      maxHeight = dims.maxHeight || null;
    }
    notify("ui/notifications/initialized");
    requestAnimationFrame(sendSize);   // и до первых результатов: «Ожидание…» тоже занимает место
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
