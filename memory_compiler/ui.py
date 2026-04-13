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
</style>
</head>
<body>
<div class="header">
<h1>Memory Compiler</h1>
<button class="theme-toggle" onclick="toggleTheme()">&#9728;/&#9790;</button>
</div>
<div class="tab-bar">
<a href="#" class="active" onclick="showTab('search');return false" id="tab-search">Поиск</a>
<a href="#" onclick="showTab('add');return false" id="tab-add">Добавить</a>
<a href="#" onclick="showTab('graph');return false" id="tab-graph">Граф</a>
<a href="#" onclick="showTab('compile');return false" id="tab-compile">Компиляция</a>
<a href="#" onclick="showTab('analytics');return false" id="tab-analytics">Аналитика</a>
</div>
<div id="view-search">
<div class="search-box">
<input id="q" type="search" placeholder="Поиск по базе знаний...">
<select id="q-project"><option value="">Все проекты</option></select>
<button onclick="doSearch()">Найти</button>
</div>
<div class="tags-bar" id="tags-bar"></div>
<div class="projects" id="projects"></div>
<div id="results"></div>
</div>
<div id="view-add" style="display:none">
<div id="save-msg"></div>
<div class="form-group"><label>Тема</label><input id="f-topic" placeholder="Краткое название"></div>
<div class="form-group"><label>Проект</label><select id="f-project"></select></div>
<div class="form-group"><label>Теги (через запятую)</label><input id="f-tags" placeholder="docker, nas, fix"></div>
<div class="form-group"><label>Содержание</label><textarea id="f-content" placeholder="Проблема, решение, ключевые факты..."></textarea></div>
<button class="btn-save" onclick="doSave()">Сохранить</button>
</div>
<div id="view-graph" style="display:none">
<div id="graph-container" style="width:100%;height:500px;border:1px solid #30363d;border-radius:8px;background:#0d1117;position:relative">
<canvas id="graph-canvas" style="width:100%;height:100%"></canvas>
</div>
<div id="graph-info" class="empty">Загрузка графа...</div>
</div>
<div id="view-compile" style="display:none">
<div id="compile-msg"></div>
<div id="compile-preview" class="card" style="display:none"><pre></pre></div>
<div style="display:flex;gap:8px;margin-top:12px">
<button class="btn-save" onclick="doCompilePreview()" style="background:#1f6feb">Превью</button>
<button class="btn-save" onclick="doCompileRun()" style="background:#238636">Применить</button>
</div>
</div>
<div id="view-analytics" style="display:none">
<div id="analytics-content"></div>
</div>
<script>
let PROJECTS=[];
fetch("/api/health").then(function(r){return r.json()}).then(function(d){PROJECTS=Object.keys(d.projects||{});renderProjects();loadTags();
$("f-project").innerHTML=PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");
$("q-project").innerHTML='<option value="">All</option>'+PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");});
const $=id=>document.getElementById(id);
let current=null;

function showTab(t){
  ["search","add","graph","compile","analytics"].forEach(v=>{
    $("view-"+v).style.display=v===t?"block":"none";
    $("tab-"+v).className=v===t?"active":"";
  });
  if(t==="graph")loadGraph();
  if(t==="analytics")loadAnalytics();
}

function renderProjects(){
  $("projects").innerHTML=PROJECTS.map(p=>
    `<a href="#" data-p="${p}" class="${p===current?'active':''}" onclick="loadProject('${p}');return false">${p}</a>`
  ).join("");
}

async function doSearch(){
  const q=$("q").value.trim();
  if(!q)return;
  current=null;renderProjects();
  const r=await fetch("/api/search?q="+encodeURIComponent(q));
  const d=await r.json();
  renderResults(d.results);
}

async function loadProject(p){
  current=p;renderProjects();$("q").value="";
  const r=await fetch("/api/projects/"+p);
  const d=await r.json();
  renderResults(d.articles);
}

async function expandCard(proj,file,el){
  const card=el.closest(".card");
  if(card.classList.contains("expanded")){card.classList.remove("expanded");el.textContent="Развернуть";return;}
  const r=await fetch("/api/article/"+proj+"/"+file);
  const d=await r.json();
  card.querySelector(".body").innerHTML=md2html(d.content||"Ошибка загрузки");
  card.classList.add("expanded");
  el.textContent="Свернуть";
}
async function deleteArticle(proj,file,el){
  if(!confirm("Удалить "+proj+"/"+file+"?"))return;
  const r=await fetch("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({project:proj,filename:file})});
  const d=await r.json();
  if(d.result){el.closest(".card").remove();}
  else{alert(d.error||"Ошибка удаления");}
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
  if(!items||!items.length){$("results").innerHTML='<div class="empty">Ничего не найдено</div>';return;}
  $("results").innerHTML=items.map(i=>{
    const bc=`<div class="breadcrumb"><a href="#" onclick="loadProject('${esc(i.project)}');return false">${esc(i.project)}</a> &rsaquo; ${esc(i.file)}</div>`;
    return `<div class="card">${bc}<h3>${esc(i.title)}</h3><div class="meta">${esc(i.project||"")} &middot; ${esc(i.file)}${i.score?' &middot; score: '+i.score:''}</div><div class="body">${md2html(i.preview)}</div><div class="actions"><span class="expand" onclick="expandCard('${esc(i.project)}','${esc(i.file)}',this)">Развернуть</span><button class="btn-del" onclick="deleteArticle('${esc(i.project)}','${esc(i.file)}',this)">Удалить</button></div></div>`;
  }).join("");
}

async function doSave(){
  const topic=$("f-topic").value.trim();
  const content=$("f-content").value.trim();
  const project=$("f-project").value;
  const tags=$("f-tags").value.trim();
  if(!topic||!content){$("save-msg").innerHTML='<div class="msg err">Заполните тему и содержание</div>';return;}
  const r=await fetch("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({topic,content,project,tags})});
  const d=await r.json();
  if(d.result){$("save-msg").innerHTML=`<div class="msg ok">${esc(d.result)}</div>`;$("f-topic").value="";$("f-content").value="";$("f-tags").value="";}
  else{$("save-msg").innerHTML=`<div class="msg err">${esc(d.error||"Ошибка")}</div>`;}
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

// Tags bar
async function loadTags(){
  const r=await fetch("/api/tags");
  const d=await r.json();
  $("tags-bar").innerHTML=d.tags.slice(0,20).map(t=>
    `<span class="tag-chip" onclick="searchByTag('${esc(t.tag)}')">${esc(t.tag)} (${t.count})</span>`
  ).join("");
}
function searchByTag(tag){$("q").value=tag;doSearch();}

// Graph visualization with interaction
let graphData=null,graphNodes=[],graphNmap={};
async function loadGraph(){
  $("graph-info").textContent="Загрузка...";
  const r=await fetch("/api/graph");
  graphData=await r.json();
  $("graph-info").textContent=`${graphData.nodes.length} статей, ${graphData.edges.length} связей. Клик по узлу — открыть статью.`;
  setupGraph();
}
function setupGraph(){
  if(!graphData)return;
  const c=$("graph-canvas");
  const W=c.parentElement.clientWidth;
  const H=Math.max(400,Math.min(600,window.innerHeight-200));
  c.width=W;c.height=H;
  graphNodes=graphData.nodes.map((n,i)=>({...n,x:W/2+Math.cos(i*2.39)*W*0.35,y:H/2+Math.sin(i*2.39)*H*0.35,vx:0,vy:0}));
  graphNmap={};graphNodes.forEach(n=>graphNmap[n.id]=n);
  for(let iter=0;iter<80;iter++){
    graphNodes.forEach(a=>{graphNodes.forEach(b=>{
      if(a===b)return;
      let dx=a.x-b.x,dy=a.y-b.y,d=Math.sqrt(dx*dx+dy*dy)||1;
      if(d<80){let f=(80-d)*0.04;a.vx+=dx/d*f;a.vy+=dy/d*f;}
    });});
    graphData.edges.forEach(e=>{
      const s=graphNmap[e.source],t=graphNmap[e.target];
      if(!s||!t)return;
      let dx=t.x-s.x,dy=t.y-s.y,d=Math.sqrt(dx*dx+dy*dy)||1;
      let f=(d-120)*0.008*e.weight;
      s.vx+=dx/d*f;s.vy+=dy/d*f;t.vx-=dx/d*f;t.vy-=dy/d*f;
    });
    graphNodes.forEach(n=>{n.x+=n.vx*0.5;n.y+=n.vy*0.5;n.vx*=0.8;n.vy*=0.8;
      n.x=Math.max(40,Math.min(W-40,n.x));n.y=Math.max(40,Math.min(H-40,n.y));});
  }
  renderGraph();
  // Click handler
  c.onclick=function(ev){
    const rect=c.getBoundingClientRect();
    const mx=ev.clientX-rect.left,my=ev.clientY-rect.top;
    for(const n of graphNodes){
      const r=Math.max(5,Math.min(12,4+n.access_count));
      if(Math.hypot(n.x-mx,n.y-my)<r+5){
        const [proj,file]=n.id.split("/",2);
        showTab("search");loadProject(proj);
        return;
      }
    }
  };
  // Hover handler
  c.onmousemove=function(ev){
    const rect=c.getBoundingClientRect();
    const mx=ev.clientX-rect.left,my=ev.clientY-rect.top;
    let found=false;
    for(const n of graphNodes){
      const r=Math.max(5,Math.min(12,4+n.access_count));
      if(Math.hypot(n.x-mx,n.y-my)<r+5){
        c.style.cursor="pointer";
        c.title=n.title+" ("+n.project+")";
        found=true;break;
      }
    }
    if(!found){c.style.cursor="default";c.title="";}
  };
}
function renderGraph(){
  if(!graphData)return;
  const c=$("graph-canvas");
  const ctx=c.getContext("2d");
  ctx.clearRect(0,0,c.width,c.height);
  ctx.globalAlpha=0.25;
  graphData.edges.forEach(e=>{
    const s=graphNmap[e.source],t=graphNmap[e.target];
    if(!s||!t)return;
    ctx.strokeStyle=getComputedStyle(document.body).getPropertyValue("color")||"#30363d";
    ctx.lineWidth=Math.max(1,e.weight*2.5);
    ctx.beginPath();ctx.moveTo(s.x,s.y);ctx.lineTo(t.x,t.y);ctx.stroke();
  });
  ctx.globalAlpha=1;
  graphNodes.forEach(n=>{
    const r=Math.max(5,Math.min(12,4+n.access_count));
    ctx.fillStyle=n.color;ctx.beginPath();ctx.arc(n.x,n.y,r,0,Math.PI*2);ctx.fill();
    ctx.strokeStyle="rgba(255,255,255,0.3)";ctx.lineWidth=1;ctx.stroke();
    ctx.fillStyle=getComputedStyle(document.body).color||"#c9d1d9";
    ctx.font="10px -apple-system,sans-serif";ctx.textAlign="center";
    ctx.fillText(n.title.substring(0,25),n.x,n.y-r-5);
  });
}

// Compile
async function doCompilePreview(){
  $("compile-msg").innerHTML='<div class="msg ok">Загрузка...</div>';
  const r=await fetch("/api/compile/preview");
  const d=await r.json();
  $("compile-preview").style.display="block";
  $("compile-preview").querySelector("pre").textContent=d.preview;
  $("compile-msg").innerHTML="";
}
async function doCompileRun(){
  if(!confirm("Применить компиляцию?"))return;
  $("compile-msg").innerHTML='<div class="msg ok">Компиляция...</div>';
  const r=await fetch("/api/compile/run",{method:"POST"});
  const d=await r.json();
  $("compile-msg").innerHTML=`<div class="msg ok">${esc(d.result)}</div>`;
  $("compile-preview").style.display="none";
}

// Analytics
async function loadAnalytics(){
  $("analytics-content").innerHTML='<div class="empty">Загрузка...</div>';
  const r=await fetch("/api/analytics");
  const d=await r.json();
  let h=`<div class="card"><h3>Статистика</h3><pre>Всего статей: ${d.total_articles}\nОтслеживается: ${d.total_tracked}\nНикогда не открывались: ${d.never_accessed.length}</pre></div>`;
  if(d.top_accessed.length){
    h+=`<div class="card"><h3>Топ по обращениям</h3>`;
    d.top_accessed.forEach(i=>{
      h+=`<div style="padding:4px 0;border-bottom:1px solid #21262d"><span style="color:#58a6ff">${esc(i.title)}</span> <span style="color:#8b949e">${i.project} &middot; ${i.access_count} обр.</span></div>`;
    });
    h+=`</div>`;
  }
  if(d.never_accessed.length){
    h+=`<div class="card"><h3>Никогда не открывались</h3><pre>${d.never_accessed.join("\\n")}</pre></div>`;
  }
  $("analytics-content").innerHTML=h;
}

$("q").addEventListener("keydown",e=>{if(e.key==="Enter")doSearch()});
// projects loaded dynamically from /api/health
</script>
</body>
</html>"""
