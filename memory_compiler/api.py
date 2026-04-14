"""REST API endpoints and Starlette application factory."""
import asyncio
from contextlib import asynccontextmanager

import numpy as np
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from memory_compiler.config import (
    KNOWLEDGE_DIR, PROJECTS, article_meta, load_article_meta, stats,
    _discover_projects, MC_API_KEY,
)
from memory_compiler import search as _search_mod
from memory_compiler.search import (
    whoosh_search, rebuild_index, rebuild_embeddings,
    load_embeddings, get_index,
)
from memory_compiler.storage import project_dir, regenerate_index, git_init, read_audit_log, decrypt_content, is_encrypted
from memory_compiler.handlers import compile as _compile, save_lesson, delete_article, lint as _lint
from memory_compiler.ui import WEB_HTML, LOGIN_HTML


# ─── Web endpoints ──────────────────────────────────────────────────────────


async def web_index(request: Request):
    return HTMLResponse(WEB_HTML)


async def web_search(request: Request):
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"results": []})
    results = whoosh_search(q, limit=15)
    return JSONResponse({"results": results})


async def web_project(request: Request):
    project = request.path_params["project"]
    proj_path = KNOWLEDGE_DIR / project
    if not proj_path.exists():
        return JSONResponse({"articles": []})
    articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for a in articles[:30]:
        text = a.read_text(encoding="utf-8")
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() if lines else a.stem
        preview = "\n".join(lines[:10])
        items.append({"title": title, "project": project, "file": a.name, "preview": preview})
    return JSONResponse({"articles": items})


async def web_article(request: Request):
    """Get full article text, with decryption for secrets."""
    project = request.path_params["project"]
    filename = request.path_params["filename"]
    fpath = KNOWLEDGE_DIR / project / filename
    if not fpath.exists() or not fpath.suffix == ".md":
        return JSONResponse({"error": "not found"}, status_code=404)
    text = fpath.read_text(encoding="utf-8")
    # Decrypt secret articles for authorized web users
    if "ENC:" in text:
        lines_dec = []
        for line in text.splitlines():
            if line.strip().startswith("ENC:"):
                lines_dec.append(decrypt_content(line.strip()))
            else:
                lines_dec.append(line)
        text = "\n".join(lines_dec)
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else filename
    return JSONResponse({"title": title, "project": project, "file": filename, "content": text})


async def web_save(request: Request):
    """Save a new lesson via web form."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    topic = data.get("topic", "").strip()
    content = data.get("content", "").strip()
    project = data.get("project", "general").strip()
    tags = data.get("tags", [])
    if not topic or not content:
        return JSONResponse({"error": "topic and content required"}, status_code=400)
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    result = await save_lesson(topic, content, project, tags)
    return JSONResponse({"result": result[0].text})


async def web_health(request: Request):
    import memory_compiler.config as _cfg
    _cfg.PROJECTS = _discover_projects()
    ix = get_index()
    total_chars = 0
    total_articles = 0
    project_stats = {}
    for proj in _cfg.PROJECTS:
        p = KNOWLEDGE_DIR / proj
        if not p.exists():
            continue
        articles = list(p.glob("*.md"))
        chars = sum(a.stat().st_size for a in articles)
        project_stats[proj] = {"articles": len(articles), "size_kb": round(chars / 1024, 1)}
        total_chars += chars
        total_articles += len(articles)
    daily_dir = KNOWLEDGE_DIR / "daily"
    daily_count = len(list(daily_dir.glob("*.md"))) if daily_dir.exists() else 0
    return JSONResponse({
        "status": "ok",
        "documents": ix.doc_count(),
        "embeddings": len(_search_mod._embeddings),
        "total_articles": total_articles,
        "total_size_kb": round(total_chars / 1024, 1),
        "daily_logs": daily_count,
        "projects": project_stats,
        "usage": stats,
    })


async def web_graph(request: Request):
    """Knowledge graph -- nodes from filesystem, edges from embeddings."""
    nodes = []
    edges = []
    palette = ["#3B82F6", "#10B981", "#F59E0B", "#8B5CF6", "#EC4899", "#F97316", "#6B7280", "#EF4444", "#14B8A6", "#A855F7"]
    proj_colors = {p: palette[i % len(palette)] for i, p in enumerate(PROJECTS)}

    # Collect ALL articles from filesystem
    all_keys = []
    for proj in PROJECTS:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            key = f"{proj}/{md.name}"
            text = md.read_text(encoding="utf-8")
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else md.stem
            # Extract tags
            tags = ""
            for line in lines[:10]:
                if line.startswith("**Теги:**"):
                    tags = line.replace("**Теги:**", "").strip()
                    break
            meta = article_meta.get(key, {})
            all_keys.append(key)
            nodes.append({
                "id": key, "title": title, "project": proj,
                "color": proj_colors.get(proj, "#6B7280"),
                "access_count": meta.get("access_count", 0),
                "tags": tags,
            })

    # Build edges from embeddings (similarity > 0.5)
    emb_keys = [k for k in all_keys if k in _search_mod._embeddings]
    for i, k1 in enumerate(emb_keys):
        for k2 in emb_keys[i+1:]:
            sim = float(np.dot(_search_mod._embeddings[k1], _search_mod._embeddings[k2]))
            if sim > 0.5:
                edges.append({"source": k1, "target": k2, "weight": round(sim, 2)})

    # Also connect articles sharing tags (for those without embeddings)
    from collections import defaultdict
    tag_index = defaultdict(list)
    for n in nodes:
        if n["tags"]:
            for t in n["tags"].split(","):
                t = t.strip().lower()
                if t and t != "—":
                    tag_index[t].append(n["id"])
    edge_set = {(e["source"], e["target"]) for e in edges}
    for tag, ids in tag_index.items():
        if len(ids) > 10:
            continue  # skip overly common tags
        for i, a in enumerate(ids):
            for b in ids[i+1:]:
                if (a, b) not in edge_set and (b, a) not in edge_set:
                    edges.append({"source": a, "target": b, "weight": 0.35})
                    edge_set.add((a, b))

    return JSONResponse({"nodes": nodes, "edges": edges})


async def web_analytics(request: Request):
    """Analytics -- article access stats."""
    load_article_meta()
    items = []
    for path, meta in article_meta.items():
        title = _search_mod._embed_texts.get(path, path.split("/")[-1] if "/" in path else path)
        proj = path.split("/")[0] if "/" in path else "unknown"
        items.append({
            "path": path, "title": title, "project": proj,
            "access_count": meta.get("access_count", 0),
            "last_accessed": meta.get("last_accessed", ""),
        })
    items.sort(key=lambda x: x["access_count"], reverse=True)

    # Never accessed articles
    all_articles = set()
    for proj in PROJECTS:
        p = KNOWLEDGE_DIR / proj
        if p.exists():
            for md in p.glob("*.md"):
                if not md.name.startswith("_"):
                    all_articles.add(f"{proj}/{md.name}")
    never_accessed = all_articles - set(article_meta.keys())

    return JSONResponse({
        "top_accessed": items[:20],
        "never_accessed": sorted(never_accessed)[:20],
        "total_tracked": len(article_meta),
        "total_articles": len(all_articles),
    })


async def web_compile_preview(request: Request):
    """Preview what compile would do -- with diffs."""
    result = await _compile(dry_run=True)
    return JSONResponse({"preview": result[0].text})


async def web_compile_run(request: Request):
    """Execute compile."""
    result = await _compile(dry_run=False)
    return JSONResponse({"result": result[0].text})


async def web_export(request: Request):
    """Export all articles from a project as JSON."""
    project = request.path_params["project"]
    proj_path = KNOWLEDGE_DIR / project
    if not proj_path.exists():
        return JSONResponse({"articles": []})
    articles = []
    for md in sorted(proj_path.glob("*.md")):
        if md.name.startswith("_"):
            continue
        text = md.read_text(encoding="utf-8")
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() if lines else md.stem
        articles.append({"filename": md.name, "title": title, "content": text})
    return JSONResponse({
        "project": project,
        "count": len(articles),
        "articles": articles,
    })


async def web_delete_article(request: Request):
    """Delete an article via web UI."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    project = data.get("project", "").strip()
    filename = data.get("filename", "").strip()
    if not project or not filename:
        return JSONResponse({"error": "project and filename required"}, status_code=400)
    result = await delete_article(project, filename)
    return JSONResponse({"result": result[0].text})


async def web_tags(request: Request):
    """Get all tags with counts."""
    tag_counts: dict[str, int] = {}
    for proj in PROJECTS:
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            text = md.read_text(encoding="utf-8")
            for line in text.splitlines()[:10]:
                if line.lower().startswith("**\u0442\u0435\u0433\u0438:**"):
                    tags_str = line.split(":", 1)[1].strip()
                    for t in tags_str.split(","):
                        t = t.strip().lower().strip("*").strip()
                        if t and t != "\u2014":
                            tag_counts[t] = tag_counts.get(t, 0) + 1
                    break
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    return JSONResponse({"tags": [{"tag": t, "count": c} for t, c in sorted_tags]})


class AuthMiddleware:
    """Pure ASGI middleware (compatible with SSE/streaming)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        if not MC_API_KEY:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        query = scope.get("query_string", b"").decode()

        # Public routes
        if path in ("/login", "/api/auth/login", "/api/health"):
            return await self.app(scope, receive, send)

        # OAuth discovery (mcp-remote probes this) — return 404
        if path.startswith("/.well-known/"):
            response = JSONResponse({"error": "not found"}, status_code=404)
            return await response(scope, receive, send)

        # SSE/MCP + /messages/ — pass through (auth via key in SSE URL)
        if path == "/sse" or path.startswith("/messages"):
            return await self.app(scope, receive, send)

        # Extract token from headers, cookies, or query param
        token = None
        headers = dict((k.decode(), v.decode()) for k, v in scope.get("headers", []))
        auth_header = headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        if not token:
            # Parse cookies
            cookie_str = headers.get("cookie", "")
            for part in cookie_str.split(";"):
                part = part.strip()
                if part.startswith("mc_token="):
                    token = part[9:]
                    break

        if not token:
            # Parse query string
            from urllib.parse import parse_qs
            params = parse_qs(query)
            token = params.get("key", [None])[0]

        if token == MC_API_KEY:
            return await self.app(scope, receive, send)

        # Unauthorized
        accept = headers.get("accept", "")
        if "text/html" in accept:
            response = RedirectResponse("/login")
        else:
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await response(scope, receive, send)


async def web_login(request: Request):
    if request.method == "GET":
        return HTMLResponse(LOGIN_HTML)
    # POST
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if data.get("key") == MC_API_KEY:
        response = JSONResponse({"ok": True})
        response.set_cookie("mc_token", MC_API_KEY, max_age=30 * 24 * 3600, httponly=True, samesite="lax")
        return response
    return JSONResponse({"error": "Неверный ключ"}, status_code=401)


async def web_audit(request: Request):
    entries = read_audit_log(100)
    return JSONResponse({"entries": entries})


# ─── Starlette app factory ─────────────────────────────────────────────────


def create_starlette_app(mcp_server: Server) -> Starlette:
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )

    async def auto_compile_loop():
        """Run compile daily at 2 AM."""
        from datetime import datetime
        while True:
            now = datetime.now()
            # Next 2 AM
            target = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if target <= now:
                target = target.replace(day=target.day + 1)
            wait = (target - now).total_seconds()
            await asyncio.sleep(wait)
            try:
                result = await _compile(dry_run=False)
                print(f"Auto-compile: {result[0].text}")
            except Exception as e:
                print(f"Auto-compile error: {e}")

    async def auto_lint_loop():
        """Run lint weekly on Sunday at 3 AM."""
        from datetime import datetime, timedelta
        while True:
            now = datetime.now()
            # Next Sunday 3 AM
            days_ahead = (6 - now.weekday()) % 7
            target = (now + timedelta(days=days_ahead)).replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=7)
            wait = (target - now).total_seconds()
            await asyncio.sleep(wait)
            try:
                result = await _lint(project="all", fix=True)
                print(f"Auto-lint: {result[0].text[:500]}")
            except Exception as e:
                print(f"Auto-lint error: {e}")

    @asynccontextmanager
    async def lifespan(app):
        git_init()
        load_article_meta()
        count = rebuild_index()
        print(f"Whoosh index built: {count} documents")
        if not load_embeddings() or len(_search_mod._embeddings) != count:
            ecount = rebuild_embeddings()
            print(f"Embeddings built: {ecount} documents")
        else:
            print(f"Embeddings loaded from cache: {len(_search_mod._embeddings)} documents")
        task = asyncio.create_task(auto_compile_loop())
        lint_task = asyncio.create_task(auto_lint_loop())
        print("Auto-compile scheduled daily at 02:00, auto-lint weekly Sun 03:00")
        yield
        task.cancel()
        lint_task.cancel()

    middleware = []
    if MC_API_KEY:
        middleware.append(Middleware(AuthMiddleware))

    return Starlette(
        routes=[
            Route("/login", endpoint=web_login, methods=["GET", "POST"]),
            Route("/api/auth/login", endpoint=web_login, methods=["POST"]),
            Route("/api/audit", endpoint=web_audit),
            Route("/", endpoint=web_index),
            Route("/api/health", endpoint=web_health),
            Route("/api/search", endpoint=web_search),
            Route("/api/save", endpoint=web_save, methods=["POST"]),
            Route("/api/article/{project}/{filename}", endpoint=web_article),
            Route("/api/projects/{project}", endpoint=web_project),
            Route("/api/graph", endpoint=web_graph),
            Route("/api/analytics", endpoint=web_analytics),
            Route("/api/compile/preview", endpoint=web_compile_preview),
            Route("/api/compile/run", endpoint=web_compile_run, methods=["POST"]),
            Route("/api/export/{project}", endpoint=web_export),
            Route("/api/delete", endpoint=web_delete_article, methods=["POST"]),
            Route("/api/tags", endpoint=web_tags),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )
