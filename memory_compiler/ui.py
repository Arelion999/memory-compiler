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
<a href="#" onclick="showTab('audit');return false" id="tab-audit">Аудит</a>
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
<div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
<select id="graph-project" onchange="filterGraph()" style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg2);color:var(--text);font-size:13px">
<option value="">Все проекты</option>
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
<button class="btn-save" onclick="doCompilePreview()" style="background:#1f6feb">Превью</button>
<button class="btn-save" onclick="doCompileRun()" style="background:#238636">Применить</button>
</div>
</div>
<div id="view-analytics" style="display:none">
<div id="analytics-content"></div>
</div>
<div id="view-audit" style="display:none">
<div id="audit-content"></div>
</div>
<script>
let PROJECTS=[];
fetch("/api/health").then(function(r){return r.json()}).then(function(d){PROJECTS=Object.keys(d.projects||{});renderProjects();loadTags();
$("f-project").innerHTML=PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");
$("q-project").innerHTML='<option value="">All</option>'+PROJECTS.map(function(p){return '<option value="'+p+'">'+p+'</option>'}).join("");});
const $=id=>document.getElementById(id);
let current=null;

function showTab(t){
  ["search","add","graph","compile","analytics","audit"].forEach(v=>{
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

// Animated graph (Obsidian-style) with zoom, pan, drag
let graphRaw=null,graphNodes=[],graphEdges=[],graphNmap={},graphAnim=null;
let gZoom=1,gPanX=0,gPanY=0,gDrag=null,gHover=null,gPanning=false,gPanStart=null;
let gFilterProject="";
async function loadGraph(){
  $("graph-info").textContent="Загрузка...";
  const r=await fetch("/api/graph");
  graphRaw=await r.json();
  // Populate project filter
  const sel=$("graph-project");
  const projs=[...new Set(graphRaw.nodes.map(n=>n.project))].sort();
  sel.innerHTML='<option value="">Все проекты</option>'+projs.map(p=>'<option value="'+p+'">'+p+'</option>').join("");
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
  $("graph-info").textContent=graphNodes.length+" статей, "+graphEdges.length+" связей"+(orphans?" · "+orphans+" без связей":"");
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

async function loadAudit(){
  $("audit-content").innerHTML='<div class="empty">Загрузка...</div>';
  const r=await fetch("/api/audit");
  const d=await r.json();
  if(!d.entries||!d.entries.length){$("audit-content").innerHTML='<div class="empty">Нет записей</div>';return;}
  let h='<div class="card"><h3>Аудит (последние '+d.entries.length+')</h3>';
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
