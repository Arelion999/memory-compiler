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
.ask-src .src{color:var(--text2);font-size:0.72em;margin-top:6px}
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
<div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
<select id="graph-project" onchange="filterGraph()" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:13px">
<option value="" data-i18n="lbl.allProjects">Все проекты</option>
</select>
<span id="graph-info" style="color:var(--text2);font-size:13px;padding:6px 0"></span>
</div>
<div id="graph-container" style="width:100%;height:600px;border:1px solid var(--border);border-radius:8px;background:var(--bg);position:relative;overflow:hidden">
<canvas id="graph-canvas"></canvas>
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
// Плейсхолдер подставляет api.py тем же приёмом, что и /*PYGMENTS_CSS*/.
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
    "ask.secretFragment":"[зашифровано — откройте статью для просмотра]",
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
    "ask.secretFragment":"[encrypted — open the article to view]",
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

// Animated graph (Obsidian-style) with zoom, pan, drag
let graphRaw=null,graphNodes=[],graphEdges=[],graphNmap={},graphAnim=null;
let gZoom=1,gPanX=0,gPanY=0,gDrag=null,gHover=null,gPanning=false,gPanStart=null;
let gFilterProject="";
async function loadGraph(){
  $("graph-info").textContent=t("msg.loading");
  const r=await fetch("/api/graph");
  graphRaw=await r.json();
  // Populate project filter
  const sel=$("graph-project");
  const projs=[...new Set(graphRaw.nodes.map(n=>n.project))].sort();
  sel.innerHTML='<option value="">'+t("lbl.allProjects")+'</option>'+projs.map(p=>'<option value="'+p+'">'+p+'</option>').join("");
  filterGraph();
}
function filterGraph(){
  gFilterProject=$("graph-project").value;
  const filtered=gFilterProject?graphRaw.nodes.filter(n=>n.project===gFilterProject):graphRaw.nodes;
  const ids=new Set(filtered.map(n=>n.id));
  graphEdges=graphRaw.edges.filter(e=>ids.has(e.source)&&ids.has(e.target));
  const c=$("graph-canvas");const cont=c.parentElement;
  const W=cont.clientWidth;const H=cont.clientHeight||600;
  c.width=W*2;c.height=H*2;c.style.width=W+"px";c.style.height=H+"px";
  // Init positions — spread by project clusters
  const projIdx={};let pi=0;
  graphNodes=filtered.map((n,i)=>{
    if(!(n.project in projIdx))projIdx[n.project]=pi++;
    const cl=projIdx[n.project];const a=cl*2.4+i*0.15;
    const cx=W+Math.cos(a)*(200+cl*80);const cy=H+Math.sin(a)*(200+cl*80);
    return{...n,x:cx+(Math.random()-0.5)*100,y:cy+(Math.random()-0.5)*100,vx:0,vy:0};
  });
  graphNmap={};graphNodes.forEach(n=>graphNmap[n.id]=n);
  gZoom=1;gPanX=0;gPanY=0;
  const orphans=graphNodes.filter(n=>n.orphan).length;
  $("graph-info").textContent=graphNodes.length+" "+t("graph.articles")+", "+graphEdges.length+" "+t("graph.links")+(orphans?" · "+orphans+" "+t("graph.orphans"):"");
  if(graphAnim)cancelAnimationFrame(graphAnim);
  tickGraph();
  setupGraphEvents();
}
function tickGraph(){
  const alpha=0.3;const N=graphNodes.length;
  // Repulsion (Barnes-Hut simplified)
  for(let i=0;i<N;i++){
    const a=graphNodes[i];
    for(let j=i+1;j<N;j++){
      const b=graphNodes[j];
      let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy;
      if(d2<1)d2=1;
      const d=Math.sqrt(d2);
      const rep=Math.min(2000/d2,3)*alpha;
      a.vx+=dx/d*rep;a.vy+=dy/d*rep;
      b.vx-=dx/d*rep;b.vy-=dy/d*rep;
    }
  }
  // Attraction (springs)
  graphEdges.forEach(e=>{
    const s=graphNmap[e.source],t=graphNmap[e.target];
    if(!s||!t)return;
    let dx=t.x-s.x,dy=t.y-s.y,d=Math.sqrt(dx*dx+dy*dy)||1;
    const f=(d-200)*0.002*e.weight*alpha;
    s.vx+=dx/d*f;s.vy+=dy/d*f;t.vx-=dx/d*f;t.vy-=dy/d*f;
  });
  // Center gravity
  const c=$("graph-canvas");const cx=c.width/2,cy=c.height/2;
  graphNodes.forEach(n=>{
    if(n===gDrag)return;
    n.vx+=(cx-n.x)*0.0005*alpha;n.vy+=(cy-n.y)*0.0005*alpha;
    n.vx*=0.85;n.vy*=0.85;
    n.x+=n.vx;n.y+=n.vy;
  });
  renderGraph();
  graphAnim=requestAnimationFrame(tickGraph);
}
function renderGraph(){
  const c=$("graph-canvas");const ctx=c.getContext("2d");
  ctx.clearRect(0,0,c.width,c.height);
  ctx.save();
  ctx.translate(c.width/2+gPanX*2,c.height/2+gPanY*2);
  ctx.scale(gZoom,gZoom);
  ctx.translate(-c.width/2,-c.height/2);
  // Edges
  ctx.globalAlpha=0.15;
  graphEdges.forEach(e=>{
    const s=graphNmap[e.source],t=graphNmap[e.target];
    if(!s||!t)return;
    ctx.strokeStyle="#58a6ff";ctx.lineWidth=Math.max(1,e.weight*3);
    ctx.beginPath();ctx.moveTo(s.x,s.y);ctx.lineTo(t.x,t.y);ctx.stroke();
  });
  // Highlight hovered node connections
  if(gHover){
    ctx.globalAlpha=0.6;ctx.strokeStyle="#58a6ff";ctx.lineWidth=2;
    graphEdges.forEach(e=>{
      if(e.source!==gHover.id&&e.target!==gHover.id)return;
      const s=graphNmap[e.source],t=graphNmap[e.target];
      if(!s||!t)return;
      ctx.beginPath();ctx.moveTo(s.x,s.y);ctx.lineTo(t.x,t.y);ctx.stroke();
    });
  }
  ctx.globalAlpha=1;
  // Nodes
  graphNodes.forEach(n=>{
    const baseR=Math.max(6,Math.min(16,5+(n.access_count||0)*0.5));
    const r=n.orphan?baseR*0.6:baseR;
    const isActive=gHover&&(gHover.id===n.id||graphEdges.some(e=>(e.source===gHover.id&&e.target===n.id)||(e.target===gHover.id&&e.source===n.id)));
    const dimmed=gHover&&!isActive;
    ctx.globalAlpha=dimmed?0.2:(n.orphan?0.5:1);
    ctx.fillStyle=n.orphan?"#6B7280":n.color;ctx.beginPath();ctx.arc(n.x,n.y,r,0,Math.PI*2);ctx.fill();
    if(n===gHover||n===gDrag){ctx.strokeStyle="#fff";ctx.lineWidth=2;ctx.stroke();}
    // Label
    const fontSize=Math.max(10,Math.min(14,11/gZoom));
    ctx.font=fontSize+"px -apple-system,sans-serif";ctx.textAlign="center";
    ctx.fillStyle=dimmed?"rgba(200,200,200,0.2)":"rgba(200,210,220,0.9)";
    const label=n.title.length>35?n.title.substring(0,33)+"…":n.title;
    ctx.fillText(label,n.x,n.y-r-6);
  });
  ctx.globalAlpha=1;
  // Tooltip for hovered node
  if(gHover){
    const r=Math.max(6,Math.min(16,5+(gHover.access_count||0)*0.5));
    const lines=[gHover.title,gHover.project+(gHover.tags?" · "+gHover.tags.substring(0,40):"")];
    const tw=Math.max(...lines.map(l=>l.length))*7+16;
    const tx=gHover.x-tw/2,ty=gHover.y+r+12;
    ctx.fillStyle="rgba(30,40,55,0.95)";ctx.strokeStyle="#58a6ff";ctx.lineWidth=1;
    ctx.beginPath();ctx.roundRect(tx,ty,tw,lines.length*18+12,6);ctx.fill();ctx.stroke();
    ctx.fillStyle="#c9d1d9";ctx.font="12px -apple-system,sans-serif";ctx.textAlign="left";
    lines.forEach((l,i)=>ctx.fillText(l,tx+8,ty+16+i*18));
  }
  ctx.restore();
}
function setupGraphEvents(){
  const c=$("graph-canvas");
  function canvasXY(ev){
    const rect=c.getBoundingClientRect();
    const sx=(ev.clientX-rect.left)*2,sy=(ev.clientY-rect.top)*2;
    const wx=(sx-c.width/2-gPanX*2)/gZoom+c.width/2;
    const wy=(sy-c.height/2-gPanY*2)/gZoom+c.height/2;
    return[wx,wy];
  }
  function findNode(wx,wy){
    for(const n of graphNodes){
      const r=Math.max(6,Math.min(16,5+(n.access_count||0)*0.5));
      if(Math.hypot(n.x-wx,n.y-wy)<r+8)return n;
    }return null;
  }
  c.onmousedown=function(ev){
    const[wx,wy]=canvasXY(ev);const n=findNode(wx,wy);
    if(n){gDrag=n;gDrag.vx=0;gDrag.vy=0;c.style.cursor="grabbing";}
    else{gPanning=true;gPanStart={x:ev.clientX-gPanX,y:ev.clientY-gPanY};c.style.cursor="move";}
  };
  c.onmousemove=function(ev){
    if(gDrag){const[wx,wy]=canvasXY(ev);gDrag.x=wx;gDrag.y=wy;gDrag.vx=0;gDrag.vy=0;return;}
    if(gPanning&&gPanStart){gPanX=ev.clientX-gPanStart.x;gPanY=ev.clientY-gPanStart.y;return;}
    const[wx,wy]=canvasXY(ev);const n=findNode(wx,wy);
    gHover=n;c.style.cursor=n?"pointer":"default";
  };
  c.onmouseup=function(){
    if(gDrag){gDrag=null;c.style.cursor="default";}
    gPanning=false;gPanStart=null;
  };
  c.ondblclick=function(ev){
    const[wx,wy]=canvasXY(ev);const n=findNode(wx,wy);
    if(n){const[proj]=n.id.split("/",2);showTab("search");loadProject(proj);}
  };
  c.onwheel=function(ev){
    ev.preventDefault();
    const delta=ev.deltaY>0?0.9:1.1;
    gZoom=Math.max(0.2,Math.min(5,gZoom*delta));
  };
  // Touch support
  let touchDist=0;
  c.ontouchstart=function(ev){
    if(ev.touches.length===1){
      const t=ev.touches[0];const rect=c.getBoundingClientRect();
      const sx=(t.clientX-rect.left)*2,sy=(t.clientY-rect.top)*2;
      const wx=(sx-c.width/2-gPanX*2)/gZoom+c.width/2;
      const wy=(sy-c.height/2-gPanY*2)/gZoom+c.height/2;
      const n=findNode(wx,wy);
      if(n){gDrag=n;}else{gPanning=true;gPanStart={x:t.clientX-gPanX,y:t.clientY-gPanY};}
    }else if(ev.touches.length===2){
      touchDist=Math.hypot(ev.touches[0].clientX-ev.touches[1].clientX,ev.touches[0].clientY-ev.touches[1].clientY);
    }
  };
  c.ontouchmove=function(ev){
    ev.preventDefault();
    if(ev.touches.length===2&&touchDist){
      const d=Math.hypot(ev.touches[0].clientX-ev.touches[1].clientX,ev.touches[0].clientY-ev.touches[1].clientY);
      gZoom=Math.max(0.2,Math.min(5,gZoom*(d/touchDist)));touchDist=d;return;
    }
    if(ev.touches.length!==1)return;
    const t=ev.touches[0];
    if(gDrag){const rect=c.getBoundingClientRect();const sx=(t.clientX-rect.left)*2,sy=(t.clientY-rect.top)*2;
      gDrag.x=(sx-c.width/2-gPanX*2)/gZoom+c.width/2;gDrag.y=(sy-c.height/2-gPanY*2)/gZoom+c.height/2;gDrag.vx=0;gDrag.vy=0;}
    else if(gPanning&&gPanStart){gPanX=t.clientX-gPanStart.x;gPanY=t.clientY-gPanStart.y;}
  };
  c.ontouchend=function(){gDrag=null;gPanning=false;gPanStart=null;touchDist=0;};
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
      const frag=i.secret?t("ask.secretFragment"):esc(i.fragment);
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
<button onclick="doLogin()">Войти</button>
<div class="error" id="err"></div>
</div>
<script>
async function doLogin(){
  const key=document.getElementById("key").value.trim();
  if(!key)return;
  const r=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key})});
  if(r.ok){location.href="/";}
  else{const d=await r.json();const e=document.getElementById("err");e.textContent=d.error||"Ошибка";e.style.display="block";}
}
document.getElementById("key").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin()});
</script>
</body>
</html>"""
