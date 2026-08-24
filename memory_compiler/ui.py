"""Web UI HTML template."""

WEB_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memory Compiler</title>
<style>
:root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;--text:#c9d1d9;--text2:#8b949e;--accent:#58a6ff;--green:#238636;--red:#da3633}
[data-theme=light]{--bg:#fff;--bg2:#f6f8fa;--bg3:#e1e4e8;--border:#d0d7de;--text:#24292f;--text2:#57606a;--accent:#0969da;--green:#1a7f37;--red:#cf222e}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);padding:12px;max-width:720px;margin:0 auto}
h1{font-size:1.3em;margin-bottom:12px;color:var(--accent)}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.theme-toggle{background:none;border:1px solid var(--border);border-radius:6px;padding:4px 8px;color:var(--text2);cursor:pointer;font-size:14px}
.search-box{display:flex;gap:8px;margin-bottom:12px}
.search-box input{flex:1;padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:16px}
.search-box button{padding:10px 16px;border:none;border-radius:6px;background:var(--green);color:#fff;font-size:14px;cursor:pointer}
.search-box select{padding:10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:14px}
.tags-bar{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tag-chip{padding:3px 10px;border-radius:12px;background:var(--bg3);color:var(--accent);font-size:12px;cursor:pointer;border:1px solid var(--border)}
.tag-chip.active{background:var(--accent);color:#fff}
.projects{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.projects a{padding:6px 12px;border-radius:16px;background:var(--bg3);color:var(--accent);text-decoration:none;font-size:13px;border:1px solid var(--border)}
.projects a.active{background:#1f6feb;color:#fff}
.breadcrumb{font-size:0.8em;color:var(--text2);margin-bottom:8px}
.breadcrumb a{color:var(--accent);text-decoration:none}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:10px;position:relative}
.card h3{font-size:0.95em;color:var(--accent);margin-bottom:6px}
.card .meta{font-size:0.8em;color:var(--text2);margin-bottom:8px}
.card .body{white-space:pre-wrap;font-size:0.85em;color:var(--text);line-height:1.5;max-height:200px;overflow-y:auto}
/* Голый <pre> — ПРЯМОЙ потомок карточки: превью компиляции и блоки аналитики.
   Своих стилей у него не было вовсе, браузерный дефолт white-space:pre не переносит
   строки — длинные («Уже в статье: «заголовок» → файл.md», списки имён файлов)
   вылезали за правый край карточки. Селектор именно ПРЯМОЙ (.card>pre), чтобы не
   задеть блоки кода внутри .body.rendered: там перенос не нужен и стоит своё
   правило с overflow-x. pre-wrap + break-word переносят, overflow-x — страховка
   на случай неразрывной строки (длинный путь без пробелов). */
.card>pre{white-space:pre-wrap;word-break:break-word;overflow-x:auto;margin:0;font-size:0.85em;line-height:1.5;color:var(--text)}
.snippet{background:#1a2332;border-left:3px solid var(--accent);padding:6px 10px;margin:6px 0;font-size:0.8em;font-family:ui-monospace,monospace;white-space:pre-wrap;word-break:break-word;border-radius:4px;line-height:1.4}
[data-theme=light] .snippet{background:#f0f6fc}
mark{background:#ffeb3b80;color:inherit;padding:0 2px;border-radius:2px;font-weight:600}
.card .body h1,.card .body h2,.card .body h3{color:var(--accent);margin:8px 0 4px}
.card .body strong{color:var(--text)}
.card .body code{background:var(--bg3);padding:1px 4px;border-radius:3px;font-size:0.9em}
.card.expanded .body{max-height:none}
.card .actions{display:flex;gap:8px;margin-top:6px;align-items:center}
.card .expand{color:var(--accent);font-size:0.8em;cursor:pointer}
.card .btn-del{color:var(--red);font-size:0.75em;cursor:pointer;border:none;background:none;padding:2px 6px}
.empty{color:var(--text2);text-align:center;padding:40px 0}
.tab-bar{display:flex;gap:0;margin-bottom:16px;border-bottom:1px solid var(--border);overflow-x:auto}
.tab-bar a{padding:8px 12px;color:var(--text2);text-decoration:none;font-size:13px;border-bottom:2px solid transparent;white-space:nowrap}
.tab-bar a.active{color:var(--accent);border-bottom-color:var(--accent)}
.form-group{margin-bottom:12px}
.form-group label{display:block;font-size:0.85em;color:var(--text2);margin-bottom:4px}
.form-group input,.form-group textarea,.form-group select{width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:14px;font-family:inherit}
.form-group textarea{min-height:120px;resize:vertical}
.btn-save{padding:10px 20px;border:none;border-radius:6px;background:var(--green);color:#fff;font-size:14px;cursor:pointer;width:100%}
.msg{padding:8px 12px;border-radius:6px;margin-bottom:12px;font-size:0.9em}
.msg.ok{background:#1a3a1a;color:#3fb950;border:1px solid var(--green)}
.msg.err{background:#3a1a1a;color:#f85149;border:1px solid var(--red)}
/* Отрендеренный Markdown (серверный HTML) */
.card .body.rendered{white-space:normal;word-break:normal}
.card .body.rendered p{margin:6px 0}
.card .body.rendered h1,.card .body.rendered h2,.card .body.rendered h3,.card .body.rendered h4,.card .body.rendered h5,.card .body.rendered h6{color:var(--accent);margin:10px 0 4px;line-height:1.3}
.card .body.rendered h1{font-size:1.25em}
.card .body.rendered h2{font-size:1.15em}
.card .body.rendered h3{font-size:1.05em}
.card .body.rendered h4,.card .body.rendered h5,.card .body.rendered h6{font-size:1em}
.card .body.rendered ul,.card .body.rendered ol{margin:6px 0 6px 22px}
.card .body.rendered li{margin:2px 0}
.card .body.rendered blockquote{border-left:3px solid var(--border);margin:8px 0;padding:2px 10px;color:var(--text2)}
.card .body.rendered code{background:var(--bg3);padding:1px 4px;border-radius:3px;font-size:0.9em;font-family:ui-monospace,monospace}
.card .body.rendered pre{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;overflow-x:auto;margin:8px 0;white-space:pre;line-height:1.4}
.card .body.rendered pre code{background:none;padding:0;white-space:pre;display:block}
.card .body.rendered a{color:var(--accent);text-decoration:none}
.card .body.rendered a:hover{text-decoration:underline}
.card .body.rendered hr{border:none;border-top:1px solid var(--border);margin:12px 0}
.card .body.rendered img{max-width:100%}
.card .body.rendered del,.card .body.rendered s{opacity:0.6}
.card .body.rendered table{border-collapse:collapse;margin:8px 0;font-size:0.9em;display:block;overflow-x:auto}
.card .body.rendered th,.card .body.rendered td{border:1px solid var(--border);padding:4px 8px;text-align:left}
.card .body.rendered th{background:var(--bg3)}
/* Командная палитра (Ctrl+K) */
.cmdk-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;z-index:1000;align-items:flex-start;justify-content:center}
.cmdk-overlay.open{display:flex}
.cmdk-box{background:var(--bg2);border:1px solid var(--border);border-radius:10px;width:92%;max-width:600px;margin-top:12vh;box-shadow:0 12px 48px rgba(0,0,0,.5);overflow:hidden}
.cmdk-box input{width:100%;padding:14px 16px;border:none;border-bottom:1px solid var(--border);background:transparent;color:var(--text);font-size:16px;outline:none}
.cmdk-results{max-height:52vh;overflow-y:auto}
.cmdk-item{padding:9px 16px;cursor:pointer;border-bottom:1px solid var(--border)}
.cmdk-item:last-child{border-bottom:none}
.cmdk-item.sel{background:var(--bg3)}
.cmdk-item .t{color:var(--accent);font-size:0.92em}
.cmdk-item .m{color:var(--text2);font-size:0.75em;margin-top:2px}
.cmdk-empty{padding:16px;color:var(--text2);text-align:center;font-size:0.85em}
.cmdk-hint{padding:6px 16px;color:var(--text2);font-size:0.72em;border-top:1px solid var(--border);display:flex;gap:14px}
/* Related-notes сайдбар */
.related{position:fixed;right:12px;top:76px;width:262px;max-height:72vh;display:none;flex-direction:column;background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow:hidden;z-index:900}
.related.open{display:flex}
.related-head{display:flex;align-items:center;gap:6px;padding:8px 10px;border-bottom:1px solid var(--border);font-size:0.8em}
.related-head .ttl{flex:1;color:var(--accent)}
.related-head button{background:none;border:1px solid var(--border);border-radius:5px;color:var(--text2);cursor:pointer;font-size:11px;padding:2px 6px}
.related-head button.on{color:var(--accent);border-color:var(--accent)}
.related-list{overflow-y:auto}
.related-item{padding:7px 10px;cursor:pointer;border-bottom:1px solid var(--border)}
.related-item:last-child{border-bottom:none}
.related-item:hover{background:var(--bg3)}
.related-item .t{color:var(--accent);font-size:0.8em;line-height:1.3}
.related-item .m{color:var(--text2);font-size:0.7em;margin-top:2px;display:flex;justify-content:space-between;gap:6px}
.related-bar{height:3px;background:var(--bg3);border-radius:2px;margin-top:4px;overflow:hidden}
.related-bar i{display:block;height:100%;background:var(--accent)}
.related-empty{padding:12px 10px;color:var(--text2);font-size:0.78em;text-align:center}
@media(max-width:1099px){.related{position:static;width:auto;max-height:none;margin:12px 0}}
/* Timeline-слайдер версий (bi-temporal снимки tracking-статьи) */
.timeline{border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin:8px 0;background:var(--bg)}
.tl-head{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:0.78em;color:var(--text2);margin-bottom:4px}
.tl-head .tl-pos{color:var(--accent);white-space:nowrap}
.tl-range{width:100%;margin:2px 0}
.tl-when{font-size:0.72em;color:var(--text2);margin:2px 0 6px}
.tl-facts{display:flex;flex-direction:column;gap:3px}
.tl-row{display:flex;gap:8px;font-size:0.78em}
.tl-row .k{color:var(--text2);min-width:92px;flex-shrink:0}
.tl-row .v{color:var(--text);word-break:break-word}
.tl-row.changed .v{color:var(--accent);font-weight:600}
.tl-row.changed .k::after{content:" \\2022";color:var(--accent)}
/* Вкладка «Ответы» (retrieval с источниками, без генерации) */
.ask-note{font-size:0.75em;color:var(--text2);margin-bottom:10px;padding:6px 10px;border-left:3px solid var(--border);background:var(--bg2);border-radius:0 4px 4px 0}
.ask-fallback{font-size:0.78em;color:var(--accent);margin-bottom:8px}
.ask-src{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px}
.ask-src .h{display:flex;justify-content:space-between;gap:8px;align-items:baseline;margin-bottom:5px}
.ask-src .h .t{color:var(--accent);font-size:0.9em;cursor:pointer}
.ask-src .h .t:hover{text-decoration:underline}
.ask-src .h .s{color:var(--text2);font-size:0.72em;white-space:nowrap}
.ask-src .frag{font-size:0.82em;line-height:1.5;color:var(--text);white-space:pre-wrap;word-break:break-word;border-left:3px solid var(--accent);padding-left:8px}
.ask-src .secret-btn{background:none;border:1px solid var(--border);border-radius:5px;color:var(--text2);cursor:pointer;font-size:0.9em;padding:1px 7px;margin-left:6px}
.ask-src .secret-btn:hover{color:var(--accent);border-color:var(--accent)}
.ask-src .src{color:var(--text2);font-size:0.72em;margin-top:6px}
/* Граф: широкая вкладка, адаптивная сцена, панель управления */
body.wide{max-width:min(1600px,100%)}
.graph-bar{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap;align-items:center}
.graph-bar select{padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:13px}
.graph-bar input{flex:1;min-width:140px;max-width:260px;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:13px}
.graph-bar .info{color:var(--text2);font-size:13px;margin-left:auto;white-space:nowrap}
#graph-container{position:relative;width:100%;height:clamp(400px,78vh,1000px);border:1px solid var(--border);border-radius:12px;background:var(--bg);overflow:hidden;touch-action:none;cursor:grab}
#graph-container.grabbing{cursor:grabbing}
#graph-container:fullscreen{height:100vh;border-radius:0}
#graph-canvas{display:block;width:100%;height:100%}
.graph-ctl{position:absolute;right:10px;bottom:10px;display:flex;flex-direction:column;gap:6px}
.graph-ctl button{width:32px;height:32px;border:1px solid var(--border);border-radius:8px;background:var(--bg2);color:var(--text2);font-size:15px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;opacity:0.85}
.graph-ctl button:hover{color:var(--accent);border-color:var(--accent);opacity:1}
.graph-legend{position:absolute;left:10px;bottom:10px;display:flex;flex-wrap:wrap;gap:4px 10px;max-width:calc(100% - 70px);font-size:11px;color:var(--text2);pointer-events:none}
.graph-legend span{display:flex;align-items:center;gap:4px}
.graph-legend i{width:8px;height:8px;border-radius:50%;display:inline-block}
@media(max-width:640px){#graph-container{height:min(64vh,560px)}.graph-bar .info{margin-left:0;width:100%}.graph-legend{display:none}}
/*PYGMENTS_CSS*/
</style>
</head>
<body>
<div class="header">
<h1>Memory Compiler <span id="version-badge" style="font-size:0.55em;color:var(--text2);font-weight:400;margin-left:6px"></span></h1>
<button class="theme-toggle" onclick="toggleTheme()">&#9728;/&#9790;</button>
<button class="theme-toggle" onclick="toggleLang()" title="RU / EN">RU/EN</button>
</div>
<div class="tab-bar">
<a href="#" class="active" onclick="showTab('search');return false" id="tab-search" data-i18n="tab.search">Поиск</a>
<a href="#" onclick="showTab('ask');return false" id="tab-ask" data-i18n="tab.ask">Ответы</a>
<a href="#" onclick="showTab('add');return false" id="tab-add" data-i18n="tab.add">Добавить</a>
<a href="#" onclick="showTab('graph');return false" id="tab-graph" data-i18n="tab.graph">Граф</a>
<a href="#" onclick="showTab('compile');return false" id="tab-compile" data-i18n="tab.compile">Компиляция</a>
<a href="#" onclick="showTab('analytics');return false" id="tab-analytics" data-i18n="tab.analytics">Аналитика</a>
<a href="#" onclick="showTab('audit');return false" id="tab-audit" data-i18n="tab.audit">Аудит</a>
</div>
<div id="view-search">
<div class="search-box">
<input id="q" type="search" data-i18n-ph="ph.search" placeholder="Поиск по базе знаний...">
<select id="q-project" onchange="onProjectChange()"><option value="" data-i18n="lbl.allProjects">Все проекты</option></select>
<button onclick="doSearch()" data-i18n="btn.find">Найти</button>
</div>
<div class="tags-bar" id="tags-bar"></div>
<div class="projects" id="projects"></div>
<div id="results"></div>
</div>
<div id="view-ask" style="display:none">
<div class="search-box">
<input id="ask-q" type="search" data-i18n-ph="ph.ask" placeholder="Вопрос по базе знаний...">
<select id="ask-project"><option value="" data-i18n="lbl.allProjects">Все проекты</option></select>
<button onclick="doAsk()" data-i18n="btn.ask">Спросить</button>
</div>
<div class="ask-note" data-i18n="ask.note">Ответ собирается из найденных фрагментов базы — это поиск с источниками, а не сгенерированный текст: LLM на сервере нет.</div>
<div id="ask-results"></div>
</div>
<div id="view-add" style="display:none">
<div id="save-msg"></div>
<div class="form-group"><label data-i18n="lbl.topic">Тема</label><input id="f-topic" data-i18n-ph="ph.topic" placeholder="Краткое название"></div>
<div class="form-group"><label data-i18n="lbl.project">Проект</label><select id="f-project"></select></div>
<div class="form-group"><label data-i18n="lbl.tags">Теги (через запятую)</label><input id="f-tags" placeholder="docker, nas, fix"></div>
<div class="form-group"><label data-i18n="lbl.content">Содержание</label><textarea id="f-content" data-i18n-ph="ph.content" placeholder="Проблема, решение, ключевые факты..."></textarea></div>
<button class="btn-save" onclick="doSave()" data-i18n="btn.save">Сохранить</button>
</div>
<div id="view-graph" style="display:none">
<div class="graph-bar">
<select id="graph-project" onchange="filterGraph()">
<option value="" data-i18n="lbl.allProjects">Все проекты</option>
</select>
<input id="graph-search" oninput="graphHighlight(this.value)" data-i18n-ph="ph.graphSearch" placeholder="Подсветить...">
<span id="graph-info" class="info"></span>
</div>
<div id="graph-container">
<canvas id="graph-canvas"></canvas>
<div class="graph-legend" id="graph-legend"></div>
<div class="graph-ctl">
<button onclick="graphZoom(1.35)" data-i18n-title="graph.zoomIn" title="Приблизить">+</button>
<button onclick="graphZoom(0.74)" data-i18n-title="graph.zoomOut" title="Отдалить">&minus;</button>
<button onclick="graphFit()" data-i18n-title="graph.fit" title="Вписать">&#9633;</button>
<button onclick="graphReload()" data-i18n-title="graph.reload" title="Обновить данные">&#8635;</button>
<button onclick="graphFullscreen()" data-i18n-title="graph.fullscreen" title="Во весь экран">&#10530;</button>
</div>
</div>
</div>
<div id="view-compile" style="display:none">
<div id="compile-msg"></div>
<div id="compile-preview" class="card" style="display:none"><pre></pre></div>
<div style="display:flex;gap:8px;margin-top:12px">
<button class="btn-save" onclick="doCompilePreview()" style="background:#1f6feb" data-i18n="btn.preview">Превью</button>
<button class="btn-save" onclick="doCompileRun()" style="background:#238636" data-i18n="btn.apply">Применить</button>
</div>
</div>
<div id="view-analytics" style="display:none">
<div id="analytics-content"></div>
</div>
<div id="view-audit" style="display:none">
<div id="audit-content"></div>
</div>
<div class="cmdk-overlay" id="cmdk" onclick="if(event.target===this)closeCmdk()">
<div class="cmdk-box">
<input id="cmdk-input" type="search" data-i18n-ph="ph.searchDots" placeholder="Поиск по базе знаний…" autocomplete="off">
<div class="cmdk-results" id="cmdk-results"></div>
<div class="cmdk-hint"><span>&uarr;&darr; <span data-i18n="cmdk.nav">навигация</span></span><span>&crarr; <span data-i18n="cmdk.open">открыть</span></span><span data-i18n="cmdk.esc">Esc закрыть</span></div>
</div>
</div>
<div class="related" id="related">
<div class="related-head">
<span class="ttl" data-i18n="lbl.related">Похожие</span>
<button id="related-play" onclick="toggleRelatedPause()" data-i18n="lbl.watching" data-i18n-title="title.freeze" title="Заморозить список: не переключаться на другую статью при переходах">следит</button>
<button onclick="closeRelated()" data-i18n-title="title.close" title="Закрыть">&times;</button>
</div>
<div class="related-list" id="related-list"></div>
</div>
<script>
// ─── i18n ───────────────────────────────────────────────────────────────
// Язык с сервера (MC_LANG) — дефолт; выбор пользователя в localStorage его перебивает.
// Значение подставляет api.py — тем же приёмом, что и CSS подсветки.
// ⚠️ НЕ упоминай здесь имя плейсхолдера буквально, даже в комментарии: api.py делает
// str.replace по ВСЕМУ документу и подставит на это место содержимое. Так и было —
// многострочный CSS влился в однострочный //-комментарий, всё после первой строки
// стало кодом, скрипт умер с «Unexpected token '{'», и UI перестал видеть данные.
var SERVER_LANG="/*MC_LANG*/";
/* i18n-dict */
var I18N={
  ru:{
    "tab.search":"Поиск",
    "tab.ask":"Ответы",
    "tab.add":"Добавить",
    "tab.graph":"Граф",
    "tab.compile":"Компиляция",
    "tab.analytics":"Аналитика",
    "tab.audit":"Аудит",
    "btn.find":"Найти",
    "btn.ask":"Спросить",
    "btn.save":"Сохранить",
    "btn.preview":"Превью",
    "btn.apply":"Применить",
    "lbl.project":"Проект",
    "lbl.topic":"Тема",
    "lbl.content":"Содержание",
    "lbl.tags":"Теги (через запятую)",
    "lbl.allProjects":"Все проекты",
    "lbl.related":"Похожие",
    "lbl.watching":"следит",
    "ph.search":"Поиск по базе знаний...",
    "ph.searchDots":"Поиск по базе знаний…",
    "ph.ask":"Вопрос по базе знаний...",
    "ph.topic":"Краткое название",
    "ph.content":"Проблема, решение, ключевые факты...",
    "title.close":"Закрыть",
    "title.freeze":"Заморозить список: не переключаться на другую статью при переходах",
    "cmdk.open":"открыть",
    "cmdk.nav":"навигация",
    "cmdk.esc":"Esc закрыть",
    "ask.note":"Ответ собирается из найденных фрагментов базы — это поиск с источниками, а не сгенерированный текст: LLM на сервере нет.",
    "msg.notFound":"Ничего не найдено",
    "msg.loading":"Загрузка...",
    "msg.error":"Ошибка",
    "msg.loadError":"Ошибка загрузки",
    "msg.deleteError":"Ошибка удаления",
    "msg.fillRequired":"Заполните тему и содержание",
    "msg.noEntries":"Нет записей",
    "msg.compiling":"Компиляция...",
    "msg.matches":"совпадений",
    "card.expand":"Развернуть",
    "card.collapse":"Свернуть",
    "card.delete":"Удалить",
    "confirm.delete":"Удалить",
    "confirm.compile":"Применить компиляцию?",
    "graph.articles":"статей",
    "graph.links":"связей",
    "graph.orphans":"без связей",
    "graph.zoomIn":"Приблизить",
    "graph.zoomOut":"Отдалить",
    "graph.fit":"Вписать в экран",
    "graph.fullscreen":"Во весь экран",
    "graph.reload":"Обновить данные",
    "graph.layout":"Раскладка...",
    "ph.graphSearch":"Подсветить...",
    "analytics.stats":"Статистика",
    "analytics.totalArticles":"Всего статей",
    "analytics.tracked":"Отслеживается",
    "analytics.neverAccessed":"Никогда не открывались",
    "analytics.topAccessed":"Топ по обращениям",
    "analytics.hits":"обр.",
    "audit.recent":"Аудит (последние",
    "cmdk.startTyping":"Начните вводить запрос…",
    "lbl.frozen":"заморожен",
    "related.loading":"Загрузка…",
    "related.empty":"Похожих не нашлось",
    "related.cosine":"косинус",
    "related.barExplain":"— полоска отсчитывается от порога шума модели",
    "timeline.versions":"Версии факта",
    "timeline.current":"текущая",
    "timeline.effectiveFrom":"действует с",
    "timeline.noDate":"дата не указана",
    "timeline.to":"по",
    "timeline.toPresent":"— по сейчас",
    "ask.searching":"Ищу…",
    "ask.fallbackAll":"В выбранном проекте ничего не нашлось — показаны результаты по всем проектам.",
    "ask.secretFragment":"[зашифровано]",
    "ask.secretShow":"Показать",
    "ask.secretError":"Не удалось раскрыть",
    "ask.queryError":"Ошибка запроса"
  },
  en:{
    "tab.search":"Search",
    "tab.ask":"Answers",
    "tab.add":"Add",
    "tab.graph":"Graph",
    "tab.compile":"Compile",
    "tab.analytics":"Analytics",
    "tab.audit":"Audit",
    "btn.find":"Search",
    "btn.ask":"Ask",
    "btn.save":"Save",
    "btn.preview":"Preview",
    "btn.apply":"Apply",
    "lbl.project":"Project",
    "lbl.topic":"Topic",
    "lbl.content":"Content",
    "lbl.tags":"Tags (comma-separated)",
    "lbl.allProjects":"All projects",
    "lbl.related":"Related",
    "lbl.watching":"watching",
    "ph.search":"Search the knowledge base...",
    "ph.searchDots":"Search the knowledge base…",
    "ph.ask":"Ask the knowledge base...",
    "ph.topic":"Short title",
    "ph.content":"Problem, solution, key facts...",
    "title.close":"Close",
    "title.freeze":"Freeze the list: do not switch to another article when navigating",
    "cmdk.open":"open",
    "cmdk.nav":"navigate",
    "cmdk.esc":"Esc to close",
    "ask.note":"The answer is assembled from retrieved fragments — this is search with sources, not generated text: there is no LLM on the server.",
    "msg.notFound":"Nothing found",
    "msg.loading":"Loading...",
    "msg.error":"Error",
    "msg.loadError":"Loading error",
    "msg.deleteError":"Deletion error",
    "msg.fillRequired":"Fill in the topic and content",
    "msg.noEntries":"No entries",
    "msg.compiling":"Compiling...",
    "msg.matches":"matches",
    "card.expand":"Expand",
    "card.collapse":"Collapse",
    "card.delete":"Delete",
    "confirm.delete":"Delete",
    "confirm.compile":"Apply compilation?",
    "graph.articles":"articles",
    "graph.links":"links",
    "graph.orphans":"orphaned",
    "graph.zoomIn":"Zoom in",
    "graph.zoomOut":"Zoom out",
    "graph.fit":"Fit to screen",
    "graph.fullscreen":"Fullscreen",
    "graph.reload":"Reload data",
    "graph.layout":"Laying out...",
    "ph.graphSearch":"Highlight...",
    "analytics.stats":"Statistics",
    "analytics.totalArticles":"Total articles",
    "analytics.tracked":"Tracked",
    "analytics.neverAccessed":"Never accessed",
    "analytics.topAccessed":"Top accessed",
    "analytics.hits":"hits",
    "audit.recent":"Audit (last",
    "cmdk.startTyping":"Start typing a query…",
    "lbl.frozen":"frozen",
    "related.loading":"Loading…",
    "related.empty":"No related notes found",
    "related.cosine":"cosine",
    "related.barExplain":"— the bar is scaled from the model's noise threshold",
    "timeline.versions":"Fact versions",
    "timeline.current":"current",
    "timeline.effectiveFrom":"in effect from",
    "timeline.noDate":"date not specified",
    "timeline.to":"to",
    "timeline.toPresent":"— to present",
    "ask.searching":"Searching…",
    "ask.fallbackAll":"Nothing found in the selected project — showing results from all projects.",
    "ask.secretFragment":"[encrypted]",
    "ask.secretShow":"Show",
    "ask.secretError":"Could not reveal",
    "ask.queryError":"Query error"
  }
};
/* /i18n-dict */
var LANG=localStorage.getItem("lang")||SERVER_LANG||"ru";
if(!I18N[LANG])LANG="ru";

// Нет перевода — отдаём русский, а не пустоту: UI не должен ломаться от опечатки в ключе.
function t(k){return (I18N[LANG]&&I18N[LANG][k])||I18N.ru[k]||k;}

// Три атрибута, потому что подпись бывает текстом, плейсхолдером и подсказкой.
function applyI18N(){
  document.querySelectorAll("[data-i18n]").forEach(function(el){el.textContent=t(el.getAttribute("data-i18n"));});
  document.querySelectorAll("[data-i18n-ph]").forEach(function(el){el.placeholder=t(el.getAttribute("data-i18n-ph"));});
  document.querySelectorAll("[data-i18n-title]").forEach(function(el){el.title=t(el.getAttribute("data-i18n-title"));});
  document.documentElement.setAttribute("lang",LANG);
}

// Reload, а не перерисовка: половина подписей живёт внутри уже отрисованных карточек,
// графа и таймлайна — перерисовывать их выборочно значит продублировать логику вкладок.
function toggleLang(){
  LANG=LANG==="en"?"ru":"en";
  localStorage.setItem("lang",LANG);
  location.reload();
}

let PROJECTS=[];
fetch("/api/health").then(function(r){return r.json()}).then(function(d){PROJECTS=Object.keys(d.projects||{});renderProjects();loadTags();
if(d.version){$("version-badge").textContent="v"+d.version;}
$("f-project").innerHTML=PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");
$("q-project").innerHTML='<option value="">All</option>'+PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");
$("ask-project").innerHTML='<option value="">'+t("lbl.allProjects")+'</option>'+PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");});
const $=id=>document.getElementById(id);
let current=null;
let activeTag=null;

function showTab(t){
  ["search","ask","add","graph","compile","analytics","audit"].forEach(v=>{
    $("view-"+v).style.display=v===t?"block":"none";
    $("tab-"+v).className=v===t?"active":"";
  });
  document.body.classList.toggle("wide",t==="graph");
  if(t==="graph")loadGraph();
  if(t==="analytics")loadAnalytics();
  if(t==="audit")loadAudit();
}

function renderProjects(){
  $("projects").innerHTML=PROJECTS.map(p=>
    `<a href="#" data-p="${p}" class="${p===current?'active':''}" onclick="loadProject('${p}');return false">${p}</a>`
  ).join("");
}

let lastQueryWords=[];
async function doSearch(){
  const q=$("q").value.trim();
  if(!q)return;
  activeTag=null;current=null;renderProjects();loadTags();
  const p=$("q-project").value;
  const r=await fetch("/api/search?q="+encodeURIComponent(q)+(p?"&project="+encodeURIComponent(p):""));
  const d=await r.json();
  lastQueryWords=(d.query||q).toLowerCase().split(/[\\s,;.:]+/).filter(w=>w.length>2);
  renderResults(d.results);
}

function escRegex(w){return w.split("").map(c=>"^.*+?$()[]{}|".indexOf(c)>=0?"\\\\"+c:c).join("");}
function highlight(s){
  if(!lastQueryWords.length||!s)return s;
  let out=s;
  for(const w of lastQueryWords){
    try{
      const re=new RegExp("("+escRegex(w)+")","gi");
      out=out.replace(re,"<mark>$1</mark>");
    }catch(e){}
  }
  return out;
}

// Подсветка слов запроса по ТЕКСТОВЫМ узлам готового HTML (не ломает теги/атрибуты).
function highlightDom(root){
  if(!lastQueryWords.length)return;
  const parts=lastQueryWords.map(escRegex).filter(Boolean);
  if(!parts.length)return;
  let re;try{re=new RegExp("("+parts.join("|")+")","gi");}catch(e){return;}
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,null);
  const targets=[];let n;
  while(n=walker.nextNode()){
    const pn=n.parentNode?n.parentNode.nodeName:"";
    if(pn==="CODE"||pn==="PRE"||pn==="MARK")continue;   // не трогаем код и уже подсвеченное
    re.lastIndex=0;
    if(re.test(n.nodeValue))targets.push(n);
  }
  for(const t of targets){
    const s=t.nodeValue,frag=document.createDocumentFragment();
    let last=0,m;re.lastIndex=0;
    while(m=re.exec(s)){
      if(m.index>last)frag.appendChild(document.createTextNode(s.slice(last,m.index)));
      const mk=document.createElement("mark");mk.textContent=m[0];frag.appendChild(mk);
      last=m.index+m[0].length;
      if(re.lastIndex===m.index)re.lastIndex++;   // защита от зацикливания
    }
    if(last<s.length)frag.appendChild(document.createTextNode(s.slice(last)));
    t.parentNode.replaceChild(frag,t);
  }
}

async function loadProject(p){
  current=p;activeTag=null;renderProjects();loadTags();$("q").value="";
  const r=await fetch("/api/projects/"+p);
  const d=await r.json();
  lastQueryWords=[];renderResults(d.articles);
}

async function expandCard(proj,file,el){
  const card=el.closest(".card");
  if(card.classList.contains("expanded")){card.classList.remove("expanded");el.textContent=t("card.expand");return;}
  const r=await fetch("/api/article/"+proj+"/"+file);
  const d=await r.json();
  // Replace snippets/preview with full body, keep highlight
  const snippets=card.querySelectorAll(".snippet");
  snippets.forEach(s=>s.remove());
  let bodyEl=card.querySelector(".body");
  if(!bodyEl){bodyEl=document.createElement("div");bodyEl.className="body";card.querySelector(".meta").after(bodyEl);}
  if(d.content_html!==undefined){
    bodyEl.className="body rendered";
    bodyEl.innerHTML=d.content_html||"";
    highlightDom(bodyEl);
  }else{
    bodyEl.className="body";
    bodyEl.innerHTML=highlight(md2html(d.content||t("msg.loadError")));
  }
  card.classList.add("expanded");
  el.textContent=t("card.collapse");
  loadRelated(proj,file);   // «Похожие» следуют за раскрытой статьёй (если не заморожены)
  loadTimeline(proj,file,card);   // слайдер версий — только у tracking-статей
  // Scroll to first match
  const firstMark=bodyEl.querySelector("mark");
  if(firstMark){setTimeout(()=>firstMark.scrollIntoView({behavior:"smooth",block:"center"}),100);}
}
async function deleteArticle(proj,file,el){
  if(!confirm(t("confirm.delete")+" "+proj+"/"+file+"?"))return;
  const r=await fetch("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({project:proj,filename:file})});
  const d=await r.json();
  if(d.result){el.closest(".card").remove();}
  else{alert(d.error||t("msg.deleteError"));}
}

function md2html(s){
  return esc(s).replace(/^### (.+)$/gm,'<h3>$1</h3>')
    .replace(/^## (.+)$/gm,'<h2>$1</h2>')
    .replace(/^# (.+)$/gm,'<h1>$1</h1>')
    .replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/^- (.+)$/gm,'&bull; $1');
}
function renderResults(items){
  if(!items||!items.length){$("results").innerHTML='<div class="empty">'+t("msg.notFound")+'</div>';return;}
  $("results").innerHTML=items.map(i=>{
    const bc=`<div class="breadcrumb"><a href="#" onclick="loadProject('${esc(i.project)}');return false">${esc(i.project)}</a> &rsaquo; ${esc(i.file)}</div>`;
    let snippetHtml="";
    if(i.snippets&&i.snippets.length){
      snippetHtml=i.snippets.map(s=>'<div class="snippet">'+highlight(esc(s))+'</div>').join("");
    }else{
      snippetHtml='<div class="body">'+highlight(md2html(i.preview))+'</div>';
    }
    return `<div class="card">${bc}<h3>${highlight(esc(i.title))}</h3><div class="meta">${esc(i.project||"")} &middot; ${esc(i.file)}${i.score?' &middot; score: '+i.score:''}${i.snippets&&i.snippets.length?' &middot; '+i.snippets.length+' '+t("msg.matches"):''}</div><div class="timeline-holder"></div>${snippetHtml}<div class="actions"><span class="expand" onclick="expandCard('${esc(i.project)}','${esc(i.file)}',this)">${t("card.expand")}</span><button class="btn-del" onclick="deleteArticle('${esc(i.project)}','${esc(i.file)}',this)">${t("card.delete")}</button></div></div>`;
  }).join("");
}

async function doSave(){
  const topic=$("f-topic").value.trim();
  const content=$("f-content").value.trim();
  const project=$("f-project").value;
  const tags=$("f-tags").value.trim();
  if(!topic||!content){$("save-msg").innerHTML='<div class="msg err">'+t("msg.fillRequired")+'</div>';return;}
  const r=await fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic,content,project,tags})});
  const d=await r.json();
  if(d.result){$("save-msg").innerHTML=`<div class="msg ok">${esc(d.result)}</div>`;$("f-topic").value="";$("f-content").value="";$("f-tags").value="";}
  else{$("save-msg").innerHTML=`<div class="msg err">${esc(d.error||t("msg.error"))}</div>`;}
}

function esc(s){return s?s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"):""}

// Theme toggle
function toggleTheme(){
  const cur=document.documentElement.getAttribute("data-theme");
  const next=cur==="light"?"dark":"light";
  document.documentElement.setAttribute("data-theme",next==="dark"?"":"light");
  if(typeof graphRepaint==="function")graphRepaint();
  localStorage.setItem("theme",next);
}
(function(){const t=localStorage.getItem("theme");if(t==="light")document.documentElement.setAttribute("data-theme","light");})();
document.addEventListener("DOMContentLoaded",applyI18N);

// Tags bar
async function loadTags(){
  const p=$("q-project")?$("q-project").value:"";
  const r=await fetch("/api/tags"+(p?"?project="+encodeURIComponent(p):""));
  const d=await r.json();
  $("tags-bar").innerHTML=d.tags.slice(0,20).map(t=>
    `<span class="tag-chip${t.tag===activeTag?' active':''}" onclick="searchByTag('${esc(t.tag)}')">${esc(t.tag)} (${t.count})</span>`
  ).join("");
}
async function runTagFilter(tag){
  const p=$("q-project").value;
  const r=await fetch("/api/by-tag?tag="+encodeURIComponent(tag)+(p?"&project="+encodeURIComponent(p):""));
  const d=await r.json();
  lastQueryWords=[];renderResults(d.articles);
}
async function searchByTag(tag){
  if(activeTag===tag){activeTag=null;loadTags();$("results").innerHTML="";return;}
  activeTag=tag;$("q").value="";current=null;renderProjects();loadTags();
  runTagFilter(tag);
}
async function onProjectChange(){
  loadTags();
  const q=$("q").value.trim();
  if(q){doSearch();return;}
  if(activeTag){runTagFilter(activeTag);return;}
  const p=$("q-project").value;
  if(p){
    const r=await fetch("/api/projects/"+encodeURIComponent(p));
    const d=await r.json();lastQueryWords=[];renderResults(d.articles);
  }else{$("results").innerHTML="";}
}

// ── Граф статей: Barnes-Hut раскладка + камера + LOD-рендер ────────────────
// Физика считается в МИРОВЫХ координатах (центр 0,0), камера отдельно — поэтому
// ресайз окна и полноэкранный режим не пересчитывают раскладку, а только вид.
// Отталкивание через quadtree: наивный двойной цикл на 2800 нодах — это 4 млн
// пар КАЖДЫЙ кадр, ровно от него граф и стоял колом.
let graphRaw=null,gNodes=[],gEdges=[],gMap={},gAdj={},gAnim=null,gRO=null,gCtx=null;
let gZoom=1,gCamX=0,gCamY=0,gDrag=null,gHover=null,gPanning=false,gPanStart=null;
let gAlpha=0,gDirty=true,gDpr=1,gW=0,gH=0,gRunning=false,gHi="",gFilterProject="";
let gPointers=new Map(),gPinch=0;
let gDim=0,gDimTo=0,gFollow=false,gTick=0;  // gDim — плавное затухание при наведении
// Замер на корпусе прода (2818 нод, 12191 ребро): THETA 0.85 -> 1.15 срезает
// раскладку с 3.6 до 2.6 с при том же зазоре между нодами (медиана 16.2 -> 15.8).
const G_ALPHA_MIN=0.005,G_VDECAY=0.72,G_REP=2600,G_LEN=90;
// Большой граф считается и рисуется по УРЕЗАННОМУ профилю: на паре тысяч нод
// цена кадра упирается в пиксели (DPR²) и в обход quadtree, а не в код вокруг.
let G_DECAY=0.02,G_THETA=1.15,G_HEAVY=false;
function graphProfile(n){
  G_HEAVY=n>1200;
  G_THETA=G_HEAVY?1.8:1.15;      // обход дерева примерно вдвое дешевле
  G_DECAY=G_HEAVY?0.045:0.02;    // раскладка сходится за ~90 тиков вместо ~230
}

async function loadGraph(){
  $("graph-info").textContent=t("msg.loading");
  if(!graphRaw){
    const r=await fetch("/api/graph");
    graphRaw=await r.json();
    const sel=$("graph-project");
    const projs=[...new Set(graphRaw.nodes.map(n=>n.project))].sort();
    sel.innerHTML='<option value="">'+t("lbl.allProjects")+'</option>'+projs.map(p=>'<option value="'+p+'">'+p+'</option>').join("");
    setupGraphEvents();
  }
  filterGraph();
}

function filterGraph(){
  gFilterProject=$("graph-project").value;
  const filtered=gFilterProject?graphRaw.nodes.filter(n=>n.project===gFilterProject):graphRaw.nodes;
  const ids=new Set(filtered.map(n=>n.id));
  gEdges=graphRaw.edges.filter(e=>ids.has(e.source)&&ids.has(e.target));
  // Кластерная затравка: центры проектов по кругу, ноды — диском вокруг своего.
  // Хорошая затравка экономит сотни итераций физики.
  const cnt={};filtered.forEach(n=>{cnt[n.project]=(cnt[n.project]||0)+1;});
  const projs=Object.keys(cnt).sort((a,b)=>cnt[b]-cnt[a]);
  const ring=Math.max(260,Math.sqrt(filtered.length)*34);
  const ctr={},seen={};
  projs.forEach((p,i)=>{
    const a=i*2.399963;const rr=ring*Math.sqrt((i+0.5)/projs.length);
    ctr[p]=[Math.cos(a)*rr,Math.sin(a)*rr];seen[p]=0;
  });
  gNodes=filtered.map(n=>{
    const k=seen[n.project]++;const c=ctr[n.project];
    const a=k*2.399963,rr=Math.sqrt(k+0.5)*11;
    return{...n,x:c[0]+Math.cos(a)*rr,y:c[1]+Math.sin(a)*rr,vx:0,vy:0,r:4,lt:null,lw:0};
  });
  gMap={};gNodes.forEach((n,i)=>{n.i=i;gMap[n.id]=n;});
  // Список соседей заранее: при наведении проверка «сосед ли» была перебором всех
  // 12 тысяч рёбер ВНУТРИ цикла по нодам — 34 млн операций на кадр.
  gAdj={};gNodes.forEach(n=>{gAdj[n.id]=[];});
  gEdges=gEdges.filter(e=>{
    const s=gMap[e.source],d=gMap[e.target];
    if(!s||!d)return false;
    e.s=s;e.d=d;gAdj[e.source].push(d);gAdj[e.target].push(s);return true;
  });
  graphProfile(gNodes.length);
  // Размер ноды — по числу связей (как в Obsidian): хаб виден сразу. Открытия
  // статьи добавляют лишь малую поправку, иначе размер начинает означать две вещи.
  gNodes.forEach(n=>{
    const deg=gAdj[n.id].length;
    n.orphan=deg===0;
    n.r=Math.max(2.6,Math.min(9,2.6+Math.sqrt(deg)*1.35+Math.sqrt(n.access_count||0)*0.25));
  });
  renderLegend(projs,cnt);
  graphResize();
  if(loadLayout()){
    // Готовая раскладка: лёгкая доводка вместо полного пересчёта
    gAlpha=0.06;gFollow=true;graphStats();graphFit();graphWake();
    return;
  }
  gAlpha=1;
  warmupGraph(0);
}

// ── Кэш раскладки в localStorage ──────────────────────────────────────────
// Разложить пару тысяч нод стоит секунды, а состав базы меняется медленно —
// пересчитывать это на КАЖДОМ открытии вкладки незачем.
function layoutKey(){return "mc.graph."+(gFilterProject||"*");}
function saveLayout(){
  try{
    const pos={};
    gNodes.forEach(function(n){pos[n.id]=[Math.round(n.x),Math.round(n.y)];});
    localStorage.setItem(layoutKey(),JSON.stringify({pos:pos}));
  }catch(e){}                      // квота или приватный режим — просто без кэша
}
function loadLayout(){
  try{
    const raw=localStorage.getItem(layoutKey());
    if(!raw)return false;
    const d=JSON.parse(raw);
    if(!d||!d.pos)return false;
    let hit=0;
    gNodes.forEach(function(n){
      const p=d.pos[n.id];
      if(p){n.x=p[0];n.y=p[1];n.vx=0;n.vy=0;hit++;}
    });
    // Состав сильно разошёлся — раскладываем заново, иначе новые статьи повиснут
    // кучей поверх старой картины
    return hit>=gNodes.length*0.85;
  }catch(e){return false;}
}

function graphStats(){
  const orphans=gNodes.filter(n=>n.orphan).length;
  $("graph-info").textContent=gNodes.length+" "+t("graph.articles")+", "+gEdges.length+" "+t("graph.links")+(orphans?" · "+orphans+" "+t("graph.orphans"):"");
}

// Прогрев кусками по кадрам: раскладка успевает сойтись до первого показа, но
// вкладка при этом не висит — иначе браузер объявляет страницу зависшей.
function warmupGraph(done){
  const TOTAL=42;
  const t0=performance.now();
  let i=0;
  while(done+i<TOTAL&&(i<8||performance.now()-t0<80)){simStep();i++;}
  done+=i;
  if(done<TOTAL&&gNodes.length>1){
    $("graph-info").textContent=t("graph.layout")+" "+Math.round(done*100/TOTAL)+"%";
    requestAnimationFrame(function(){warmupGraph(done);});
    return;
  }
  graphStats();
  // Дальше граф оседает НА ГЛАЗАХ, а камера едет за ним: прогрев снимает только
  // первый бесформенный ком, остальное — часть картинки.
  gAlpha=0.55;gFollow=true;graphFit();graphWake();
}

// ── Barnes-Hut quadtree ────────────────────────────────────────────────────
function bhCell(x,y,s){return{x:x,y:y,s:s,m:0,cx:0,cy:0,kids:null,pt:null};}
function bhInsert(q,n,depth){
  q.m++;q.cx+=n.x;q.cy+=n.y;
  if(q.kids){const h=q.s/2;bhInsert(q.kids[(n.x>=q.x+h?1:0)+(n.y>=q.y+h?2:0)],n,depth+1);return;}
  if(!q.pt){q.pt=n;return;}
  if(depth>=18)return;                       // совпавшие точки: дальше не дробим
  const h=q.s/2,old=q.pt;q.pt=null;
  q.kids=[bhCell(q.x,q.y,h),bhCell(q.x+h,q.y,h),bhCell(q.x,q.y+h,h),bhCell(q.x+h,q.y+h,h)];
  bhInsert(q.kids[(old.x>=q.x+h?1:0)+(old.y>=q.y+h?2:0)],old,depth+1);
  bhInsert(q.kids[(n.x>=q.x+h?1:0)+(n.y>=q.y+h?2:0)],n,depth+1);
}
function bhApply(q,n,k){
  if(q.m===0||(q.pt===n&&q.m===1))return;
  const mx=q.cx/q.m,my=q.cy/q.m;
  let dx=n.x-mx,dy=n.y-my,d2=dx*dx+dy*dy;
  if(q.kids&&q.s*q.s>G_THETA*G_THETA*d2){
    bhApply(q.kids[0],n,k);bhApply(q.kids[1],n,k);bhApply(q.kids[2],n,k);bhApply(q.kids[3],n,k);
    return;
  }
  if(d2<25){                                  // разводим совпавшие узлы детерминированно
    const a=n.i*2.399963;dx=Math.cos(a)*5;dy=Math.sin(a)*5;d2=25;
  }
  const f=G_REP*q.m*k/(d2*Math.sqrt(d2));
  n.vx+=dx*f;n.vy+=dy*f;
}

function simStep(){
  const N=gNodes.length;
  if(!N)return;
  const k=gAlpha;
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  for(let i=0;i<N;i++){const n=gNodes[i];
    if(n.x<minX)minX=n.x;if(n.y<minY)minY=n.y;
    if(n.x>maxX)maxX=n.x;if(n.y>maxY)maxY=n.y;}
  const root=bhCell(minX,minY,Math.max(maxX-minX,maxY-minY,1)*1.02+2);
  for(let i=0;i<N;i++)bhInsert(root,gNodes[i],0);
  for(let i=0;i<N;i++)bhApply(root,gNodes[i],k);
  for(let e=0;e<gEdges.length;e++){
    const ed=gEdges[e],s=ed.s,d=ed.d;
    let dx=d.x-s.x,dy=d.y-s.y,dist=Math.sqrt(dx*dx+dy*dy)||1;
    const f=(dist-G_LEN)*0.04*ed.weight*k/dist;
    const fx=dx*f,fy=dy*f;
    s.vx+=fx;s.vy+=fy;d.vx-=fx;d.vy-=fy;
  }
  const grav=0.010*k;
  // Сцена шире, чем выше — тянем раскладку по той же пропорции, иначе круглый
  // ком оставляет половину площади пустой.
  const ar=Math.sqrt(Math.max(1,Math.min(2.6,(gW||1)/(gH||1))));
  const gx=grav/ar,gy=grav*ar;
  for(let i=0;i<N;i++){
    const n=gNodes[i];
    if(n===gDrag){n.vx=0;n.vy=0;continue;}
    n.vx-=n.x*gx;n.vy-=n.y*gy;
    n.vx*=G_VDECAY;n.vy*=G_VDECAY;
    n.x+=n.vx;n.y+=n.vy;
  }
}

// ── Камера и адаптивная сцена ──────────────────────────────────────────────
function graphResize(){
  const cont=$("graph-container");if(!cont)return;
  const w=cont.clientWidth,h=cont.clientHeight;
  if(!w||!h)return;
  gW=w;gH=h;
  // Ретина на большом графе учетверяет число пикселей на кадр — цена, которую
  // видно как лаг; на общем плане ноды всё равно точки в 3-4 пикселя.
  gDpr=Math.min(window.devicePixelRatio||1,G_HEAVY?1:2);
  const c=$("graph-canvas");
  c.width=Math.round(w*gDpr);c.height=Math.round(h*gDpr);
  gCtx=c.getContext("2d");
  gDirty=true;graphWake();
}
function graphFit(smooth){
  if(!gNodes.length)return;
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  gNodes.forEach(function(n){
    if(n.x<minX)minX=n.x;if(n.y<minY)minY=n.y;
    if(n.x>maxX)maxX=n.x;if(n.y>maxY)maxY=n.y;});
  const cx=(minX+maxX)/2,cy=(minY+maxY)/2,pad=54;
  const z=Math.max(0.05,Math.min(3,Math.min((gW-pad)/Math.max(maxX-minX,1),(gH-pad)/Math.max(maxY-minY,1))));
  if(smooth){const e=0.12;gCamX+=(cx-gCamX)*e;gCamY+=(cy-gCamY)*e;gZoom+=(z-gZoom)*e;}
  else{gCamX=cx;gCamY=cy;gZoom=z;}
  gDirty=true;graphWake();
}
function graphZoom(f,px,py){
  gFollow=false;
  const cx=px===undefined?gW/2:px,cy=py===undefined?gH/2:py;
  const wx=(cx-gW/2)/gZoom+gCamX,wy=(cy-gH/2)/gZoom+gCamY;
  gZoom=Math.max(0.05,Math.min(8,gZoom*f));
  gCamX=wx-(cx-gW/2)/gZoom;gCamY=wy-(cy-gH/2)/gZoom;
  gDirty=true;graphWake();
}
function graphFullscreen(){
  const el=$("graph-container");
  if(document.fullscreenElement)document.exitFullscreen();
  else if(el.requestFullscreen)el.requestFullscreen();
}
function graphHighlight(v){gHi=(v||"").trim().toLowerCase();gDirty=true;graphWake();}
const gMuteCache={};
function muteColor(hex,light){
  const k=hex+(light?"L":"D");
  if(gMuteCache[k])return gMuteCache[k];
  const v=parseInt(hex.slice(1),16);
  let r=(v>>16)&255,g=(v>>8)&255,b=v&255;
  const to=light?[124,134,148]:[152,164,180],m=light?0.5:0.58;
  r=Math.round(r+(to[0]-r)*m);g=Math.round(g+(to[1]-g)*m);b=Math.round(b+(to[2]-b)*m);
  return gMuteCache[k]="rgb("+r+","+g+","+b+")";
}
// Данные графа кэшируются: /api/graph считает попарные близости по всей базе и стоит
// секунд, а вкладка переключается часто. Обновление — по кнопке.
function graphReload(){
  try{localStorage.removeItem(layoutKey());}catch(e){}
  graphRaw=null;gFollow=true;loadGraph();
}
function graphRepaint(){gDirty=true;if(gNodes.length)graphWake();}
function renderLegend(projs,cnt){
  const box=$("graph-legend");if(!box)return;
  if(gFilterProject||projs.length<2){box.innerHTML="";return;}
  const seen={};gNodes.forEach(function(n){seen[n.project]=n.color;});
  box.innerHTML=projs.slice(0,10).map(function(p){
    return '<span><i style="background:'+(seen[p]||"#6B7280")+'"></i>'+p+" "+cnt[p]+"</span>";}).join("");
}

// Цикл живёт ТОЛЬКО пока есть что считать или рисовать: устоявшийся граф не
// должен жечь процессор, а прежняя версия крутила физику вечно.
function graphWake(){
  if(gRunning)return;
  gRunning=true;gAnim=requestAnimationFrame(graphLoop);
}
function graphLoop(){
  if($("view-graph").style.display==="none"){gRunning=false;return;}
  let live=false;
  if(gAlpha>G_ALPHA_MIN){
    gAlpha*=(1-G_DECAY);simStep();gDirty=true;live=true;
    if(gFollow&&(gTick++&3)===0)graphFit(true);
  }
  if(Math.abs(gDim-gDimTo)>0.01){gDim+=(gDimTo-gDim)*0.22;gDirty=true;live=true;}
  else if(gDim!==gDimTo){gDim=gDimTo;gDirty=true;}
  if(gDirty){renderGraph();gDirty=false;live=true;}
  if(!live&&!gDrag){
    gRunning=false;
    if(gNodes.length)saveLayout();
    return;
  }
  gAnim=requestAnimationFrame(graphLoop);
}

// ── Рендер ────────────────────────────────────────────────────────────────
function renderGraph(){
  const ctx=gCtx;if(!ctx)return;
  const bg=getComputedStyle(document.body).getPropertyValue("--bg").trim()||"#0d1117";
  const light=document.documentElement.getAttribute("data-theme")==="light";
  ctx.setTransform(1,0,0,1,0,0);
  ctx.clearRect(0,0,gW*gDpr,gH*gDpr);
  ctx.setTransform(gDpr,0,0,gDpr,0,0);
  ctx.fillStyle=light?"#fbfcfd":"#0b0f14";ctx.fillRect(0,0,gW,gH);
  const z=gZoom,ox=gW/2-gCamX*z,oy=gH/2-gCamY*z;
  const M=140;                                  // запас за краем: линии не обрубаются
  const hov=gHover,nb=hov?new Set(gAdj[hov.id].map(function(n){return n.id;})):null;
  // Рёбра одним путём на группу: 12 тысяч отдельных stroke() — это и был рендер-лаг
  ctx.lineCap="round";
  const strong=[],weak=[];
  for(let i=0;i<gEdges.length;i++){
    const e=gEdges[i],s=e.s,d=e.d;
    const sx=s.x*z+ox,sy=s.y*z+oy,dx=d.x*z+ox,dy=d.y*z+oy;
    if((sx<-M&&dx<-M)||(sx>gW+M&&dx>gW+M)||(sy<-M&&dy<-M)||(sy>gH+M&&dy>gH+M))continue;
    if(hov&&(e.source===hov.id||e.target===hov.id))continue;
    (e.weight>=0.6?strong:weak).push(sx,sy,dx,dy);
  }
  const line=light?"#aab4c0":"#59636f";
  [[weak,0.8,light?0.38:0.34],[strong,1.15,light?0.55:0.5]].forEach(function(grp){
    const arr=grp[0];if(!arr.length)return;
    ctx.globalAlpha=grp[2]*(1-gDim*0.72);ctx.strokeStyle=line;ctx.lineWidth=grp[1];
    ctx.beginPath();
    for(let i=0;i<arr.length;i+=4){ctx.moveTo(arr[i],arr[i+1]);ctx.lineTo(arr[i+2],arr[i+3]);}
    ctx.stroke();
  });
  if(hov){
    ctx.globalAlpha=0.85*gDim;ctx.strokeStyle="#58a6ff";ctx.lineWidth=1.6;ctx.beginPath();
    gAdj[hov.id].forEach(function(o){
      ctx.moveTo(hov.x*z+ox,hov.y*z+oy);ctx.lineTo(o.x*z+ox,o.y*z+oy);});
    ctx.stroke();
  }
  // Ноды: батч по цвету — один fill() на проект вместо тысяч
  const buckets={},labels=[];
  for(let i=0;i<gNodes.length;i++){
    const n=gNodes[i];
    const sx=n.x*z+ox,sy=n.y*z+oy;
    n.sx=sx;n.sy=sy;
    if(sx<-M||sx>gW+M||sy<-M||sy>gH+M){n.sr=0;continue;}
    const r=Math.max(1.1,Math.min(n.r*1.7,n.r*z))*(n.orphan?0.6:1);
    n.sr=r;
    const dim=hov&&n!==hov&&!nb.has(n.id);
    const key=(dim?"d|":"n|")+(n.orphan?(light?"#aab3bf":"#5b6572"):muteColor(n.color,light));
    (buckets[key]||(buckets[key]=[])).push(sx,sy,r);
    if(!dim)labels.push(n);
  }
  // Свечение: широкий полупрозрачный круг того же цвета под нодой. Отдельным
  // проходом на цвет — shadowBlur на каждой ноде стоил бы кадра.
  const keys=Object.keys(buckets);
  if(!G_HEAVY)keys.forEach(function(key){
    if(key.charAt(0)==="d")return;
    const arr=buckets[key];
    ctx.globalAlpha=(light?0.05:0.10)*(1-gDim*0.55);ctx.fillStyle=key.slice(2);
    ctx.beginPath();
    let any=false;
    for(let i=0;i<arr.length;i+=3){
      if(arr[i+2]<4)continue;                  // мелкие точки не светятся
      const gr=arr[i+2]*1.55;any=true;
      ctx.moveTo(arr[i]+gr,arr[i+1]);ctx.arc(arr[i],arr[i+1],gr,0,6.2832);}
    if(any)ctx.fill();
  });
  ctx.lineWidth=Math.min(2,1+z*0.3);ctx.strokeStyle=bg;
  keys.forEach(function(key){
    const arr=buckets[key],dim=key.charAt(0)==="d";
    ctx.globalAlpha=dim?0.98-gDim*0.84:0.98;ctx.fillStyle=key.slice(2);
    ctx.beginPath();
    for(let i=0;i<arr.length;i+=3){
      ctx.moveTo(arr[i]+arr[i+2],arr[i+1]);ctx.arc(arr[i],arr[i+1],arr[i+2],0,6.2832);}
    ctx.fill();
    if(!dim&&z>0.45)ctx.stroke();
  });
  // Совпадение с поиском — заметное кольцо
  if(gHi){
    ctx.globalAlpha=1;ctx.strokeStyle=light?"#bf8700":"#f0b849";ctx.lineWidth=2.2;
    ctx.beginPath();
    for(let i=0;i<gNodes.length;i++){
      const n=gNodes[i];
      if(!n.sr||n.title.toLowerCase().indexOf(gHi)<0)continue;
      ctx.moveTo(n.sx+n.sr+3.5,n.sy);ctx.arc(n.sx,n.sy,n.sr+3.5,0,6.2832);}
    ctx.stroke();
  }
  if(hov){
    ctx.globalAlpha=1;ctx.shadowColor="#58a6ff";ctx.shadowBlur=18;
    ctx.fillStyle=hov.color;
    ctx.beginPath();ctx.arc(hov.sx,hov.sy,hov.sr||6,0,6.2832);ctx.fill();
    ctx.shadowBlur=0;
    ctx.strokeStyle=light?"#24292f":"#fff";ctx.lineWidth=2;ctx.stroke();
  }
  drawLabels(labels,hov,nb,light,z);
  ctx.globalAlpha=1;
}

// Подписи — самая дорогая часть рендера и главный источник каши на экране:
// прежняя версия печатала имя КАЖДОЙ из 2800 статей поверх соседних.
function drawLabels(labels,hov,nb,light,z){
  const ctx=gCtx;
  ctx.font="500 11.5px -apple-system,system-ui,sans-serif";
  // Кандидаты берутся крупными вперёд и укладываются по ФАКТИЧЕСКОМУ пересечению
  // рамок: разрежение сеткой пропускало пары по разные стороны границы ячейки.
  function place(cands,budget,boxes){
    const out=[];
    cands.sort(function(a,b){return b.sr-a.sr;});
    for(let i=0;i<cands.length&&out.length<budget;i++){
      const n=cands[i];
      if(!n.sr)continue;
      if(!n.lt)n.lt=n.title.length>34?n.title.slice(0,32)+"…":n.title;
      if(!n.lw)n.lw=ctx.measureText(n.lt).width;
      const x0=n.sx-n.lw/2-3,x1=n.sx+n.lw/2+3,y1=n.sy-n.sr-4,y0=y1-13;
      let hit=false;
      for(let b=0;b<boxes.length;b++){
        const o=boxes[b];
        if(x0<o[2]&&x1>o[0]&&y0<o[3]&&y1>o[1]){hit=true;break;}
      }
      if(hit)continue;
      boxes.push([x0,y0,x1,y1]);out.push(n);
    }
    return out;
  }
  let show;
  if(hov){
    // Место сначала резервирует сама наведённая нода — её подпись не подвинуть
    if(!hov.lt)hov.lt=hov.title.length>34?hov.title.slice(0,32)+"…":hov.title;
    if(!hov.lw)hov.lw=ctx.measureText(hov.lt).width;
    const hr=hov.sr||6;
    // Вторая строка (проект и теги) шире заголовка — бронируем по ЕЁ ширине,
    // иначе сосед встаёт вплотную и надписи сливаются.
    hov.sub=hov.project+(hov.tags?" · "+hov.tags.slice(0,46):"");
    ctx.font="11px -apple-system,system-ui,sans-serif";
    const sw=Math.max(hov.lw,ctx.measureText(hov.sub).width);
    ctx.font="500 11.5px -apple-system,system-ui,sans-serif";
    const boxes=[[hov.sx-hov.lw/2-3,hov.sy-hr-17,hov.sx+hov.lw/2+3,hov.sy-hr-4],
                 [hov.sx-sw/2-3,hov.sy+hr+3,hov.sx+sw/2+3,hov.sy+hr+19]];
    show=place(gAdj[hov.id].slice(),24,boxes);
  }else{
    const budget=z<0.95?0:(z<1.5?40:(z<2.3?90:170));
    show=budget?place(labels,budget,[]):[];
  }
  ctx.globalAlpha=1;ctx.textAlign="center";ctx.textBaseline="bottom";
  ctx.lineJoin="round";ctx.miterLimit=2;
  // Подписи проявляются с зумом, а не возникают скачком
  ctx.globalAlpha=Math.max(0.4,Math.min(1,(z-0.9)*2.2));
  ctx.font="500 11.5px -apple-system,system-ui,sans-serif";
  ctx.strokeStyle=light?"rgba(255,255,255,0.9)":"rgba(6,10,16,0.9)";
  ctx.lineWidth=3.2;
  show.forEach(function(n){
    if(!n.lt)n.lt=n.title.length>34?n.title.slice(0,32)+"…":n.title;
    ctx.strokeText(n.lt,n.sx,n.sy-n.sr-5);});
  ctx.fillStyle=light?"#3c4450":"#b6c2ce";
  show.forEach(function(n){ctx.fillText(n.lt,n.sx,n.sy-n.sr-5);});
  ctx.globalAlpha=1;
  if(!hov)return;
  const hr=hov.sr||6;
  ctx.font="600 12.5px -apple-system,system-ui,sans-serif";
  ctx.strokeText(hov.lt,hov.sx,hov.sy-hr-5);
  ctx.fillStyle=light?"#0969da":"#79c0ff";
  ctx.fillText(hov.lt,hov.sx,hov.sy-hr-5);
  const sub=hov.sub||hov.project;
  ctx.font="11px -apple-system,system-ui,sans-serif";ctx.textBaseline="top";
  ctx.strokeText(sub,hov.sx,hov.sy+hr+5);
  ctx.fillStyle=light?"#57606a":"#8b949e";
  ctx.fillText(sub,hov.sx,hov.sy+hr+5);
}

// ── Ввод: указатели (мышь и тач одним кодом) ───────────────────────────────
function graphPick(px,py){
  const z=gZoom,ox=gW/2-gCamX*z,oy=gH/2-gCamY*z;
  let best=null,bd=Infinity;
  for(let i=0;i<gNodes.length;i++){
    const n=gNodes[i];
    const dx=n.x*z+ox-px,dy=n.y*z+oy-py,d=dx*dx+dy*dy;
    const rr=(n.sr||n.r)+9;
    if(d<rr*rr&&d<bd){bd=d;best=n;}
  }
  return best;
}
function setupGraphEvents(){
  const c=$("graph-canvas"),cont=$("graph-container");
  const loc=function(ev){const b=c.getBoundingClientRect();return[ev.clientX-b.left,ev.clientY-b.top];};
  c.addEventListener("pointerdown",function(ev){
    c.setPointerCapture(ev.pointerId);
    gPointers.set(ev.pointerId,loc(ev));
    if(gPointers.size===2){
      const p=[...gPointers.values()];
      gPinch=Math.hypot(p[0][0]-p[1][0],p[0][1]-p[1][1]);gPanning=false;gDrag=null;return;
    }
    const pt=loc(ev),n=graphPick(pt[0],pt[1]);
    if(n){gDrag=n;gAlpha=Math.max(gAlpha,0.28);gFollow=false;}
    else{gPanning=true;gFollow=false;gPanStart=[pt[0],pt[1],gCamX,gCamY];cont.classList.add("grabbing");}
    graphWake();
  });
  c.addEventListener("pointermove",function(ev){
    const pt=loc(ev),px=pt[0],py=pt[1];
    if(gPointers.has(ev.pointerId))gPointers.set(ev.pointerId,pt);
    if(gPointers.size===2&&gPinch){
      const p=[...gPointers.values()];
      const d=Math.hypot(p[0][0]-p[1][0],p[0][1]-p[1][1]);
      if(d>4){graphZoom(d/gPinch,(p[0][0]+p[1][0])/2,(p[0][1]+p[1][1])/2);gPinch=d;}
      return;
    }
    if(gDrag){
      gDrag.x=(px-gW/2)/gZoom+gCamX;gDrag.y=(py-gH/2)/gZoom+gCamY;
      gDrag.vx=0;gDrag.vy=0;gDirty=true;graphWake();return;
    }
    if(gPanning&&gPanStart){
      gCamX=gPanStart[2]-(px-gPanStart[0])/gZoom;
      gCamY=gPanStart[3]-(py-gPanStart[1])/gZoom;
      gDirty=true;graphWake();return;
    }
    const n=graphPick(px,py);
    if(n!==gHover){gHover=n;gDimTo=n?1:0;c.style.cursor=n?"pointer":"";gDirty=true;graphWake();}
  });
  const release=function(ev){
    gPointers.delete(ev.pointerId);
    if(gPointers.size<2)gPinch=0;
    if(gDrag){gDrag=null;gAlpha=Math.max(gAlpha,0.12);graphWake();}
    gPanning=false;gPanStart=null;cont.classList.remove("grabbing");
  };
  c.addEventListener("pointerup",release);
  c.addEventListener("pointercancel",release);
  c.addEventListener("pointerleave",function(){
    if(gHover){gHover=null;gDimTo=0;gDirty=true;graphWake();}});
  c.addEventListener("wheel",function(ev){
    ev.preventDefault();
    const pt=loc(ev);
    graphZoom(ev.deltaY>0?0.86:1.16,pt[0],pt[1]);
  },{passive:false});
  c.addEventListener("dblclick",function(ev){
    const pt=loc(ev),n=graphPick(pt[0],pt[1]);
    if(!n)return;
    const i=n.id.indexOf("/");
    openArticle(n.id.slice(0,i),n.id.slice(i+1),n.title);
  });
  // Сцена подстраивается под контейнер, а не наоборот: смена вкладки, поворот
  // телефона и полноэкранный режим меняют только вид, раскладка остаётся.
  if(window.ResizeObserver&&!gRO){
    gRO=new ResizeObserver(function(){graphResize();});
    gRO.observe(cont);
  }
  window.addEventListener("orientationchange",function(){setTimeout(graphResize,120);});
}

// Compile
async function doCompilePreview(){
  $("compile-msg").innerHTML='<div class="msg ok">'+t("msg.loading")+'</div>';
  const r=await fetch("/api/compile/preview");
  const d=await r.json();
  $("compile-preview").style.display="block";
  $("compile-preview").querySelector("pre").textContent=d.preview;
  $("compile-msg").innerHTML="";
}
async function doCompileRun(){
  if(!confirm(t("confirm.compile")))return;
  $("compile-msg").innerHTML='<div class="msg ok">'+t("msg.compiling")+'</div>';
  const r=await fetch("/api/compile/run",{method:"POST"});
  const d=await r.json();
  $("compile-msg").innerHTML=`<div class="msg ok">${esc(d.result)}</div>`;
  $("compile-preview").style.display="none";
}

// Analytics
async function loadAnalytics(){
  $("analytics-content").innerHTML='<div class="empty">'+t("msg.loading")+'</div>';
  const r=await fetch("/api/analytics");
  const d=await r.json();
  let h=`<div class="card"><h3>${t("analytics.stats")}</h3><pre>${t("analytics.totalArticles")}: ${d.total_articles}\n${t("analytics.tracked")}: ${d.total_tracked}\n${t("analytics.neverAccessed")}: ${d.never_accessed.length}</pre></div>`;
  if(d.top_accessed.length){
    h+=`<div class="card"><h3>${t("analytics.topAccessed")}</h3>`;
    d.top_accessed.forEach(i=>{
      h+=`<div style="padding:4px 0;border-bottom:1px solid #21262d"><span style="color:#58a6ff">${esc(i.title)}</span> <span style="color:#8b949e">${i.project} &middot; ${i.access_count} ${t("analytics.hits")}</span></div>`;
    });
    h+=`</div>`;
  }
  if(d.never_accessed.length){
    h+=`<div class="card"><h3>${t("analytics.neverAccessed")}</h3><pre>${d.never_accessed.join("\\n")}</pre></div>`;
  }
  $("analytics-content").innerHTML=h;
}

async function loadAudit(){
  $("audit-content").innerHTML='<div class="empty">'+t("msg.loading")+'</div>';
  const r=await fetch("/api/audit");
  const d=await r.json();
  if(!d.entries||!d.entries.length){$("audit-content").innerHTML='<div class="empty">'+t("msg.noEntries")+'</div>';return;}
  let h='<div class="card"><h3>'+t("audit.recent")+' '+d.entries.length+')</h3>';
  d.entries.reverse().forEach(e=>{
    const args=Object.entries(e.args||{}).map(([k,v])=>k+'='+JSON.stringify(v)).join(', ');
    h+='<div style="padding:4px 0;border-bottom:1px solid var(--border);font-size:0.8em">';
    h+='<span style="color:var(--text2)">'+esc(e.ts)+'</span> ';
    h+='<span style="color:var(--accent)">'+esc(e.tool)+'</span> ';
    h+='<span style="color:var(--text2)">'+esc(args).substring(0,100)+'</span> ';
    h+='<span style="color:var(--text2)">['+e.size+' chars]</span>';
    h+='</div>';
  });
  h+='</div>';
  $("audit-content").innerHTML=h;
}

// Командная палитра (Ctrl/Cmd+K)
let cmdkItems=[],cmdkSel=-1,cmdkTimer=null,cmdkSeq=0;
function openCmdk(){$("cmdk").classList.add("open");const i=$("cmdk-input");i.value="";i.focus();cmdkItems=[];cmdkSel=-1;renderCmdk();}
function closeCmdk(){$("cmdk").classList.remove("open");}
function renderCmdk(){
  const box=$("cmdk-results");
  if(!cmdkItems.length){box.innerHTML='<div class="cmdk-empty">'+($("cmdk-input").value.trim()?t("msg.notFound"):t("cmdk.startTyping"))+'</div>';return;}
  box.innerHTML=cmdkItems.map((it,idx)=>
    '<div class="cmdk-item'+(idx===cmdkSel?' sel':'')+'" onmouseenter="cmdkSel='+idx+';cmdkMark()" onclick="cmdkOpen('+idx+')">'
    +'<div class="t">'+esc(it.title)+'</div>'
    +'<div class="m">'+esc(it.project||"")+' &middot; '+esc(it.file)+(it.score?' &middot; '+it.score:'')+'</div></div>'
  ).join("");
  cmdkMark();
}
function cmdkMark(){
  const els=$("cmdk-results").querySelectorAll(".cmdk-item");
  els.forEach((e,i)=>e.classList.toggle("sel",i===cmdkSel));
  const s=els[cmdkSel];if(s)s.scrollIntoView({block:"nearest"});
}
async function cmdkSearch(){
  const q=$("cmdk-input").value.trim();
  if(!q){cmdkItems=[];cmdkSel=-1;renderCmdk();return;}
  const seq=++cmdkSeq;
  try{
    const r=await fetch("/api/search?q="+encodeURIComponent(q));
    const d=await r.json();
    if(seq!==cmdkSeq)return;   // отбросить устаревший ответ
    cmdkItems=(d.results||[]).slice(0,8);
    cmdkSel=cmdkItems.length?0:-1;
    renderCmdk();
  }catch(e){}
}
function cmdkOpen(idx){
  const it=cmdkItems[idx];if(!it)return;
  const q=$("cmdk-input").value.trim();
  closeCmdk();
  showTab("search");
  $("q").value=q;
  lastQueryWords=q.toLowerCase().split(/[\\s,;.:]+/).filter(w=>w.length>2);
  renderResults([it]);
  const exp=$("results").querySelector(".expand");   // авто-развернуть выбранную статью
  if(exp)exp.click();
}
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&(e.key==="k"||e.key==="K")){e.preventDefault();openCmdk();return;}
  if(!$("cmdk").classList.contains("open"))return;
  if(e.key==="Escape"){e.preventDefault();closeCmdk();}
  else if(e.key==="ArrowDown"){e.preventDefault();if(cmdkItems.length){cmdkSel=(cmdkSel+1)%cmdkItems.length;cmdkMark();}}
  else if(e.key==="ArrowUp"){e.preventDefault();if(cmdkItems.length){cmdkSel=(cmdkSel-1+cmdkItems.length)%cmdkItems.length;cmdkMark();}}
  else if(e.key==="Enter"&&cmdkSel>=0){e.preventDefault();cmdkOpen(cmdkSel);}
});
$("cmdk-input").addEventListener("input",()=>{clearTimeout(cmdkTimer);cmdkTimer=setTimeout(cmdkSearch,160);});

// Related-notes сайдбар (семантически близкие к раскрытой статье)
let relatedItems=[],relatedPaused=false,relatedViewing=null,relatedSeq=0;
function closeRelated(){$("related").classList.remove("open");}
function toggleRelatedPause(){
  relatedPaused=!relatedPaused;
  const b=$("related-play");
  b.textContent=relatedPaused?t("lbl.frozen"):t("lbl.watching");
  b.classList.toggle("on",relatedPaused);
  // разморозка => подхватить статью, открытую СЕЙЧАС, а не ту, на которой заморозились
  if(!relatedPaused&&relatedViewing)loadRelated(relatedViewing.project,relatedViewing.file,true);
}
function relatedOpen(idx){const i=relatedItems[idx];if(i)openArticle(i.project,i.file,i.title);}
async function loadRelated(proj,file,force){
  relatedViewing={project:proj,file:file};   // помним открытое даже когда заморожено
  if(relatedPaused&&!force)return;           // «заморожен»: список не трогаем
  const seq=++relatedSeq;
  $("related").classList.add("open");
  $("related-list").innerHTML='<div class="related-empty">'+t("related.loading")+'</div>';
  try{
    const r=await fetch("/api/related?project="+encodeURIComponent(proj)+"&file="+encodeURIComponent(file));
    const d=await r.json();
    if(seq!==relatedSeq)return;      // отбросить устаревший ответ
    relatedItems=d.related||[];
    if(!relatedItems.length){$("related-list").innerHTML='<div class="related-empty">'+t("related.empty")+'</div>';return;}
    $("related-list").innerHTML=relatedItems.map((i,idx)=>{
      // полоска — по rel (шкала от порога шума модели), число — сырой косинус:
      // рисовать полоску по сырому значению значило бы завышать связь (см. RELATED_SCORE_FLOOR)
      const rel=(typeof i.rel==="number")?i.rel:i.score;
      return '<div class="related-item" onclick="relatedOpen('+idx+')" title="'+t("related.cosine")+' '+i.score.toFixed(3)+' '+t("related.barExplain")+'">'
      +'<div class="t">'+esc(i.title)+'</div>'
      +'<div class="m"><span>'+esc(i.project)+'</span><span>'+i.score.toFixed(2)+'</span></div>'
      +'<div class="related-bar"><i style="width:'+Math.round(Math.max(0,Math.min(1,rel))*100)+'%"></i></div>'
      +'</div>';
    }).join("");
  }catch(e){$("related-list").innerHTML='<div class="related-empty">'+t("msg.loadError")+'</div>';}
}
function openArticle(proj,file,title){
  showTab("search");
  lastQueryWords=[];
  renderResults([{project:proj,file:file,title:title,preview:"",snippets:[]}]);
  const exp=$("results").querySelector(".expand");
  if(exp)exp.click();               // развернуть => expandCard подтянет тело и обновит «Похожие»
}

// Timeline-слайдер версий: прокрутка bi-temporal снимков tracking-статьи
let tlData=null,tlRoot=null;
async function loadTimeline(proj,file,card){
  const holder=card?card.querySelector(".timeline-holder"):null;
  if(!holder)return;
  holder.innerHTML="";
  try{
    const r=await fetch("/api/timeline?project="+encodeURIComponent(proj)+"&file="+encodeURIComponent(file));
    const d=await r.json();
    if(!d.snapshots||d.snapshots.length<2)return;   // один снимок нечего прокручивать
    tlData=d;
    const last=d.snapshots.length-1;
    holder.innerHTML='<div class="timeline">'
      +'<div class="tl-head"><span>'+t("timeline.versions")+(d.entity?" &middot; "+esc(d.entity):"")+'</span><span class="tl-pos"></span></div>'
      +'<input type="range" class="tl-range" min="0" max="'+last+'" value="'+last+'" oninput="renderTimeline(this.value)">'
      +'<div class="tl-when"></div><div class="tl-facts"></div></div>';
    tlRoot=holder.querySelector(".timeline");
    renderTimeline(last);
  }catch(e){holder.innerHTML="";}
}
function renderTimeline(idx){
  if(!tlData||!tlRoot)return;
  idx=+idx;
  const s=tlData.snapshots[idx],prev=idx>0?tlData.snapshots[idx-1]:null;
  tlRoot.querySelector(".tl-pos").textContent=(idx+1)+" / "+tlData.snapshots.length+(s.current?" · "+t("timeline.current"):"");
  tlRoot.querySelector(".tl-when").textContent=
    (s.from?t("timeline.effectiveFrom")+" "+s.from:t("timeline.noDate"))+(s.to?" "+t("timeline.to")+" "+s.to:(s.current?" "+t("timeline.toPresent"):""));
  tlRoot.querySelector(".tl-facts").innerHTML=tlData.fields.map(f=>{
    const v=s.facts[f],p=prev?prev.facts[f]:undefined;
    if(v===undefined&&p===undefined)return "";
    const changed=!!prev&&String(v)!==String(p);   // подсветка того, что изменилось к этому снимку
    return '<div class="tl-row'+(changed?' changed':'')+'"><span class="k">'+esc(f)+'</span>'
      +'<span class="v">'+esc(v===undefined?"—":String(v))+'</span></div>';
  }).join("");
}

// Вкладка «Ответы»: retrieval с источниками (генерации нет — LLM на сервере отсутствует)
let askItems=[];
function askOpen(idx){const i=askItems[idx];if(i)openArticle(i.project,i.file,i.title);}

// Раскрытие секрета прямо во вкладке ответов. Тела секрета в /api/ask НЕТ вовсе
// (ask_sources отдаёт пустой fragment), поэтому дотягиваем статью тем же endpoint'ом,
// что и обычное разворачивание карточки: он уже расшифровывает и уже под тем же
// ключом доступа. Отдельный endpoint выдачи plaintext не заводим — лишняя поверхность.
// Вставляем через textContent: тело секрета не должно ни рендериться как markdown,
// ни исполняться как разметка.
async function askReveal(idx,btn){
  const i=askItems[idx];if(!i)return;
  const box=btn.closest(".frag");
  btn.disabled=true;
  try{
    const r=await fetch("/api/article/"+encodeURIComponent(i.project)+"/"+encodeURIComponent(i.file));
    const d=await r.json();
    box.textContent=d.content||"";
  }catch(e){btn.disabled=false;box.textContent=t("ask.secretError");}
}
async function doAsk(){
  const q=$("ask-q").value.trim();
  if(!q)return;
  const p=$("ask-project").value;
  $("ask-results").innerHTML='<div class="empty">'+t("ask.searching")+'</div>';
  try{
    const r=await fetch("/api/ask?q="+encodeURIComponent(q)+(p?"&project="+encodeURIComponent(p):""));
    const d=await r.json();
    askItems=d.answers||[];
    if(!askItems.length){$("ask-results").innerHTML='<div class="empty">'+t("msg.notFound")+'</div>';return;}
    let h=d.fallback_all?'<div class="ask-fallback">'+t("ask.fallbackAll")+'</div>':"";
    h+=askItems.map((i,idx)=>{
      const sc="score "+i.score+((i.rerank!==null&&i.rerank!==undefined)?" · rerank "+i.rerank:"");
      const frag=i.secret
        ?t("ask.secretFragment")+'<button class="secret-btn" onclick="askReveal('+idx+',this)">'+t("ask.secretShow")+'</button>'
        :esc(i.fragment);
      return '<div class="ask-src"><div class="h"><span class="t" onclick="askOpen('+idx+')">'+esc(i.title)+'</span>'
        +'<span class="s">'+sc+'</span></div>'
        +'<div class="frag">'+frag+'</div>'
        +'<div class="src">'+esc(i.project)+' / '+esc(i.file)+'</div></div>';
    }).join("");
    $("ask-results").innerHTML=h;
  }catch(e){$("ask-results").innerHTML='<div class="empty">'+t("ask.queryError")+'</div>';}
}
$("ask-q").addEventListener("keydown",e=>{if(e.key==="Enter")doAsk()});

$("q").addEventListener("keydown",e=>{if(e.key==="Enter")doSearch()});
// projects loaded dynamically from /api/health
</script>
</body>
</html>"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memory Compiler — Вход</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#1e293b;border-radius:12px;padding:2rem;width:360px;box-shadow:0 4px 24px rgba(0,0,0,.4)}
h1{font-size:1.25rem;margin-bottom:1.5rem;text-align:center}
input{width:100%;padding:.75rem 1rem;border:1px solid #334155;border-radius:8px;background:#0f172a;color:#e2e8f0;font-size:1rem;margin-bottom:1rem}
input:focus{outline:none;border-color:#3b82f6}
button{width:100%;padding:.75rem;border:none;border-radius:8px;background:#3b82f6;color:#fff;font-size:1rem;cursor:pointer}
button:hover{background:#2563eb}
.error{color:#f87171;font-size:.875rem;margin-top:.5rem;text-align:center;display:none}
</style>
</head>
<body>
<div class="card">
<h1>Memory Compiler</h1>
<input type="password" id="key" placeholder="API Key" autofocus>
<button id="loginBtn" onclick="doLogin()">Войти</button>
<div class="error" id="err"></div>
</div>
<script>
var LANG="/*MC_LANG*/"==="en"?"en":"ru";
/* i18n-dict */
var L={ru:{title:"Вход",btn:"Войти",err:"Ошибка"},
       en:{title:"Sign in",btn:"Sign in",err:"Error"}}[LANG];
/* /i18n-dict */
document.title="Memory Compiler — "+L.title;
document.getElementById("loginBtn").textContent=L.btn;
async function doLogin(){
  const key=document.getElementById("key").value.trim();
  if(!key)return;
  const r=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key})});
  if(r.ok){location.href="/";}
  else{const d=await r.json();const e=document.getElementById("err");e.textContent=d.error||L.err;e.style.display="block";}
}
document.getElementById("key").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin()});
</script>
</body>
</html>"""
