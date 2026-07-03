"""REST API endpoints and Starlette application factory."""
import asyncio
import hmac
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
    _discover_projects, MC_API_KEY, MC_ENCRYPT_KEY, VERSION,
)
from memory_compiler import search as _search_mod
from memory_compiler.search import (
    whoosh_search, rebuild_index, rebuild_embeddings,
    load_embeddings, get_index,
)
from memory_compiler.storage import (
    project_dir, regenerate_index, git_init, read_audit_log,
    decrypt_content, is_encrypted, safe_article_path, safe_project_dir,
)
from memory_compiler.handlers import compile as _compile, save_lesson, delete_article, lint as _lint
from memory_compiler.ui import WEB_HTML, LOGIN_HTML


# ─── Web endpoints ──────────────────────────────────────────────────────────


def _is_authed(request) -> bool:
    """Несёт ли запрос валидный MC_API_KEY (Bearer или cookie). Если MC_API_KEY не
    задан — auth не настроен, доступ считается открытым (как в AuthMiddleware).
    Сравнение constant-time (hmac.compare_digest)."""
    if not MC_API_KEY:
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], MC_API_KEY):
        return True
    token = request.cookies.get("mc_token", "")
    return bool(token) and hmac.compare_digest(token, MC_API_KEY)


def _maybe_decrypt_secret_lines(text: str) -> str:
    """Дешифровать ENC:-строки для web-ответа ТОЛЬКО когда auth настроен (MC_API_KEY
    задан и эндпоинт под AuthMiddleware). Fail-closed: при пустом MC_API_KEY middleware
    не монтируется — не раскрываем секреты по HTTP, отдаём шифртекст как есть. Иначе
    шифрование секретов на диске не защищало бы ни от чего (сервер сам отдаёт plaintext)."""
    if not MC_API_KEY or "ENC:" not in text:
        return text
    out = []
    for line in text.splitlines():
        s = line.strip()
        out.append(decrypt_content(s) if s.startswith("ENC:") else line)
    return "\n".join(out)


def _check_key_hygiene() -> list:
    """Предупреждения о конфигурации ключей (находки аудита #1/#2). Список строк."""
    warns = []
    if MC_ENCRYPT_KEY and not MC_API_KEY:
        warns.append(
            "⚠️  MC_ENCRYPT_KEY задан, а MC_API_KEY пуст: REST-доступ без аутентификации, "
            "секреты по HTTP не дешифруются (fail-closed). Задайте MC_API_KEY для Web UI.")
    if MC_API_KEY and MC_API_KEY == MC_ENCRYPT_KEY:
        warns.append(
            "⚠️  MC_API_KEY == MC_ENCRYPT_KEY: ключ доступа совпадает с ключом шифрования. "
            "Он идёт в каждом запросе — его утечка раскроет ВСЕ секреты (вкл. git-историю). "
            "Задайте РАЗНЫЕ случайные ключи.")
    return warns


def _safe_proj_path(project: str):
    """Path к каталогу проекта без побочного создания; None если имя небезопасно
    (traversal) или каталога нет. Для read-only web-эндпоинтов — не плодим пустые
    каталоги, но и не даём project='..' уйти за пределы knowledge."""
    if (not project or project in (".", "..")
            or "/" in project or "\\" in project or ".." in project):
        return None
    p = KNOWLEDGE_DIR / project
    return p if p.exists() and p.is_dir() else None


async def web_index(request: Request):
    return HTMLResponse(WEB_HTML)


async def web_search(request: Request):
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"results": []})
    project = request.query_params.get("project", "").strip() or "all"
    results = whoosh_search(q, project=project, limit=15)
    # Add snippets: lines from article body that contain query words (with context)
    import re as _re
    query_words = [w.lower() for w in _re.split(r'[\s,;.:]+', q) if len(w) > 2]
    for r in results:
        fpath = KNOWLEDGE_DIR / r["project"] / r["file"]
        if not fpath.exists():
            r["snippets"] = []
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except Exception:
            r["snippets"] = []
            continue
        # Decrypt ENC: lines for snippets only under configured auth (fail-closed).
        text = _maybe_decrypt_secret_lines(text)
        lines = text.splitlines()
        snippets = []
        seen_indices = set()
        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(w in line_lower for w in query_words):
                # Context: 1 line before, the match, 1 line after
                start, end = max(0, i - 1), min(len(lines), i + 2)
                idx_set = frozenset(range(start, end))
                if idx_set & seen_indices:
                    continue
                seen_indices.update(idx_set)
                snippet_lines = lines[start:end]
                snippets.append("\n".join(snippet_lines))
                if len(snippets) >= 5:
                    break
        r["snippets"] = snippets
        r["query_words"] = query_words  # for client-side highlight
    return JSONResponse({"results": results, "query": q})


async def web_project(request: Request):
    project = request.path_params["project"]
    proj_path = _safe_proj_path(project)
    if proj_path is None:
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
    """Get full article text, with decryption for secrets (only under configured auth)."""
    project = request.path_params["project"]
    filename = request.path_params["filename"]
    try:
        fpath = safe_article_path(project, filename)  # отвергает traversal (../, абс. путь)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not fpath.exists() or fpath.suffix != ".md":
        return JSONResponse({"error": "not found"}, status_code=404)
    text = _maybe_decrypt_secret_lines(fpath.read_text(encoding="utf-8"))
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
    ix = get_index()
    payload = {"status": "ok", "version": VERSION, "documents": ix.doc_count()}
    # Детали (имена проектов = клиентов, размеры, usage-счётчики) — только под
    # настроенным auth. Публичный /api/health нужен Docker healthcheck (без токена),
    # поэтому он отдаёт лишь status/version/documents, без разведданных о базе.
    if _is_authed(request):
        import memory_compiler.config as _cfg
        _cfg.PROJECTS = _discover_projects()
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
        payload.update({
            "embeddings": len(_search_mod._embeddings),
            "total_articles": total_articles,
            "total_size_kb": round(total_chars / 1024, 1),
            "daily_logs": daily_count,
            "projects": project_stats,
            "usage": stats,
        })
    return JSONResponse(payload)


async def web_version(request: Request):
    """Simple version endpoint."""
    return JSONResponse({"version": VERSION})


async def web_graph(request: Request):
    """Knowledge graph -- nodes from filesystem, edges from embeddings."""
    nodes = []
    edges = []
    palette = ["#3B82F6", "#10B981", "#F59E0B", "#8B5CF6", "#EC4899", "#F97316", "#6B7280", "#EF4444", "#14B8A6", "#A855F7"]
    # Refresh project list from filesystem (picks up projects created in other processes)
    current_projects = _discover_projects()
    proj_colors = {p: palette[i % len(palette)] for i, p in enumerate(current_projects)}

    # Collect ALL articles from filesystem
    all_keys = []
    for proj in current_projects:
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

    # Build edges from embeddings (similarity > 0.5). Снимок под локом — иначе фоновый
    # rebuild может свопнуть _embeddings между проверкой членства и доступом (KeyError).
    emb = _search_mod.snapshot_embeddings()
    emb_keys = [k for k in all_keys if k in emb]
    for i, k1 in enumerate(emb_keys):
        for k2 in emb_keys[i+1:]:
            sim = float(np.dot(emb[k1], emb[k2]))
            if sim > 0.45:
                edges.append({"source": k1, "target": k2, "weight": round(sim, 2)})

    # Tag-based edges — only for meaningful (non-meta) tags
    from collections import defaultdict
    TAG_BLACKLIST = {
        "obsidian-import", "базаданных", "clippings", "inbox",
        "—", "", "dashboard", "главная",
    }
    tag_index = defaultdict(list)
    for n in nodes:
        if n["tags"]:
            for t in n["tags"].split(","):
                t = t.strip().lower()
                if t and t not in TAG_BLACKLIST:
                    tag_index[t].append(n["id"])

    edge_set = {(e["source"], e["target"]) for e in edges}
    for tag, ids in tag_index.items():
        # Meaningful tag: 2-15 articles
        if len(ids) < 2 or len(ids) > 15:
            continue
        for i, a in enumerate(ids):
            for b in ids[i+1:]:
                if (a, b) not in edge_set and (b, a) not in edge_set:
                    edges.append({"source": a, "target": b, "weight": 0.3})
                    edge_set.add((a, b))

    # Limit edges per node — keep only top-K strongest connections
    MAX_EDGES_PER_NODE = 8
    node_edges = defaultdict(list)
    for e in edges:
        node_edges[e["source"]].append(e)
        node_edges[e["target"]].append(e)

    kept_edges = set()  # edge ids to keep
    for nid, node_edge_list in node_edges.items():
        # Sort by weight desc, keep top K
        node_edge_list.sort(key=lambda e: -e["weight"])
        for e in node_edge_list[:MAX_EDGES_PER_NODE]:
            kept_edges.add((e["source"], e["target"]))

    edges = [e for e in edges if (e["source"], e["target"]) in kept_edges]

    # Mark orphans (no edges) — UI can dim them
    connected = set()
    for e in edges:
        connected.add(e["source"])
        connected.add(e["target"])
    for n in nodes:
        n["orphan"] = n["id"] not in connected

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
    for proj in _discover_projects():
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
    """Export all articles from a project as JSON (secrets excluded)."""
    project = request.path_params["project"]
    proj_path = _safe_proj_path(project)
    if proj_path is None:
        return JSONResponse({"articles": []})
    articles = []
    for md in sorted(proj_path.glob("*.md")):
        # Служебные (_*) и секреты (secret_*) не выгружаем — даже шифртекст.
        if md.name.startswith("_") or md.name.startswith("secret_"):
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


def _article_tags(md) -> list[str]:
    """\u0418\u0437\u0432\u043b\u0435\u0447\u044c \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u043e\u0432\u0430\u043d\u043d\u044b\u0435 \u0442\u0435\u0433\u0438 \u0438\u0437 \u0441\u0442\u0440\u043e\u043a\u0438 `**\u0422\u0435\u0433\u0438:**` \u0441\u0442\u0430\u0442\u044c\u0438 (\u043f\u0435\u0440\u0432\u044b\u0435 10 \u0441\u0442\u0440\u043e\u043a)."""
    text = md.read_text(encoding="utf-8")
    for line in text.splitlines()[:10]:
        if line.lower().startswith("**\u0442\u0435\u0433\u0438:**"):
            tags_str = line.split(":", 1)[1].strip()
            tags = []
            for t in tags_str.split(","):
                t = t.strip().lower().strip("*").strip()
                if t and t != "\u2014":
                    tags.append(t)
            return tags
    return []


def _scoped_projects(request: Request) -> list[str]:
    """\u0421\u043f\u0438\u0441\u043e\u043a \u043f\u0440\u043e\u0435\u043a\u0442\u043e\u0432 \u0441 \u0443\u0447\u0451\u0442\u043e\u043c \u043d\u0435\u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u043e\u0433\u043e ?project= (\u043f\u0443\u0441\u0442\u043e/all \u2192 \u0432\u0441\u0435)."""
    project = request.query_params.get("project", "").strip()
    if project and project != "all":
        return [project]
    return _discover_projects()


async def web_tags(request: Request):
    """Get all tags with counts, optionally scoped to ?project=."""
    tag_counts: dict[str, int] = {}
    for proj in _scoped_projects(request):
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        for md in proj_path.glob("*.md"):
            if md.name.startswith("_"):
                continue
            for t in _article_tags(md):
                tag_counts[t] = tag_counts.get(t, 0) + 1
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    return JSONResponse({"tags": [{"tag": t, "count": c} for t, c in sorted_tags]})


async def web_by_tag(request: Request):
    """\u0421\u0442\u0430\u0442\u044c\u0438 \u0441 \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u044b\u043c \u0442\u0435\u0433\u043e\u043c (?tag=, \u043e\u043f\u0446\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u043e ?project=).

    \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0435\u0442 \u0442\u043e\u0442 \u0436\u0435 \u0440\u0430\u0437\u0431\u043e\u0440 `**\u0422\u0435\u0433\u0438:**`, \u0447\u0442\u043e \u0438 \u0441\u0447\u0451\u0442\u0447\u0438\u043a \u0432 web_tags, \u043f\u043e\u044d\u0442\u043e\u043c\u0443
    \u0447\u0438\u0441\u043b\u043e \u0441\u0442\u0430\u0442\u0435\u0439 \u0432 \u0432\u044b\u0434\u0430\u0447\u0435 \u0432\u0441\u0435\u0433\u0434\u0430 \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u0435\u0442 \u0441 \u0446\u0438\u0444\u0440\u043e\u0439 \u043d\u0430 \u0447\u0438\u043f\u0435 \u0442\u0435\u0433\u0430.
    """
    tag = request.query_params.get("tag", "").strip().lower()
    if not tag:
        return JSONResponse({"articles": []})
    items = []
    for proj in _scoped_projects(request):
        proj_path = KNOWLEDGE_DIR / proj
        if not proj_path.exists():
            continue
        articles = sorted(proj_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for md in articles:
            if md.name.startswith("_"):
                continue
            if tag in _article_tags(md):
                lines = md.read_text(encoding="utf-8").splitlines()
                title = lines[0].lstrip("# ").strip() if lines else md.stem
                preview = "\n".join(lines[:10])
                items.append({"title": title, "project": proj, "file": md.name, "preview": preview})
    return JSONResponse({"articles": items, "tag": tag})


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

        # Public routes
        if path in ("/login", "/api/auth/login", "/api/health", "/api/version"):
            return await self.app(scope, receive, send)

        # OAuth discovery (mcp-remote probes this) — return 404
        if path.startswith("/.well-known/"):
            response = JSONResponse({"error": "not found"}, status_code=404)
            return await response(scope, receive, send)

        # SSE/MCP + /messages/ — pass through (auth via key in SSE URL)
        if path == "/sse" or path.startswith("/messages"):
            return await self.app(scope, receive, send)

        # Extract token from Authorization header or mc_token cookie ONLY.
        # ?key= в query убран намеренно: ключ в URL утекает в access-логи прокси/
        # uvicorn, Referer и историю браузера. (SSE-путь /sse пропущен выше отдельно.)
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

        # Constant-time сравнение — не сливаем длину/префикс ключа по таймингу.
        if token and hmac.compare_digest(token, MC_API_KEY):
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
        for _w in _check_key_hygiene():  # операционная гигиена ключей (аудит #1/#2)
            print(_w)
        # One-time migration: merge case-variant project dirs (e.g. MyProj → myproj).
        # Safe to call every startup — does nothing when no duplicates exist.
        from memory_compiler.storage import merge_case_duplicates
        merges = merge_case_duplicates()
        if merges:
            for m in merges:
                print(f"Project case-merge: {m['from']} → {m['to']} ({m['files_moved']} files)")
            # Re-discover after merge
            import memory_compiler.config as _cfg
            _cfg.PROJECTS = _cfg._discover_projects()
        load_article_meta()
        count = rebuild_index()
        print(f"Whoosh index built: {count} documents")
        # Embeddings: load from cache if compatible; otherwise rebuild in
        # background so we don't block server startup (rebuild with BGE-M3 or
        # other long-context model can take 5-15 minutes and would prevent
        # /api/health from responding, marking the container unhealthy).
        bg_rebuild_task = None  # noqa: keep reference so asyncio doesn't GC it
        if load_embeddings() and len(_search_mod._embeddings) >= 1:
            print(f"Embeddings loaded from cache: {len(_search_mod._embeddings)} documents")
            # Warn if dict grew unusually large (memory pressure check)
            n_emb = len(_search_mod._embeddings)
            if n_emb > 10000:
                print(f"⚠️ Embeddings dict has {n_emb} entries — consider archival/pruning")
        else:
            print("Embeddings cache invalid/empty — scheduling rebuild in background")

            async def _bg_rebuild():
                import asyncio as _asy
                loop = _asy.get_event_loop()
                try:
                    n = await loop.run_in_executor(None, rebuild_embeddings)
                    print(f"[bg] Embeddings rebuild done: {n} vectors")
                except Exception as e:
                    print(f"[bg] Embeddings rebuild failed: {e}")

            # Keep strong reference — asyncio.create_task can GC dangling tasks
            bg_rebuild_task = asyncio.create_task(_bg_rebuild())

        # Прогрев ML-моделей в фоне: embed-модель и cross-encoder reranker грузятся
        # лениво при ПЕРВОМ search. На NAS холодная загрузка bge-reranker-v2-m3 +
        # предикт не укладывались в MCP-таймаут клиента → первый поиск падал в -32001.
        # Грузим заранее в executor'е, не блокируя старт; к первому запросу модели готовы.
        async def _warm_models():
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, _search_mod.get_embed_model)
                await loop.run_in_executor(None, _search_mod.get_reranker_model)
                print("[warm] ML models preloaded (embed + reranker)")
            except Exception as e:
                print(f"[warm] model preload failed (lazy-load on first use): {e}")

        warm_task = asyncio.create_task(_warm_models())  # strong ref: avoid GC
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
            Route("/api/version", endpoint=web_version),
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
            Route("/api/by-tag", endpoint=web_by_tag),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )
