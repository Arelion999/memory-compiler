"""Тесты web-безопасности (v1.7.26).

Закрывают находки аудита:
  - #14 path-traversal в web-эндпоинтах (обход safe_article_path, который есть в MCP-слое);
  - #2  fail-closed: при пустом MC_API_KEY секреты НЕ дешифруются по HTTP;
  - #11 публичный /api/health не раскрывает имена проектов (= клиентов);
  - #10 ?key= в URL больше не аутентифицирует (утечка ключа в access-логи), constant-time.
"""
import asyncio
import json

from memory_compiler.api import web_article, web_export, web_project, web_health, AuthMiddleware


class Req:
    """Stand-in под starlette.Request: path/query/headers/cookies."""

    def __init__(self, path=None, query=None, headers=None, cookies=None):
        self.path_params = path or {}
        self.query_params = query or {}
        self.headers = headers or {}
        self.cookies = cookies or {}


def _json(resp):
    return json.loads(resp.body)


def _set_keys(monkeypatch, api_key, enc_key):
    import memory_compiler.config as cfg
    import memory_compiler.api as api_mod
    monkeypatch.setattr(cfg, "MC_API_KEY", api_key)
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", enc_key)
    monkeypatch.setattr(api_mod, "MC_API_KEY", api_key)


# ─── #14 path traversal ──────────────────────────────────────────────────────

def test_web_article_rejects_traversal_via_project(knowledge_dir):
    """project='..' уводит на уровень выше knowledge — web-слой строил путь напрямую,
    минуя safe_article_path (который защищает MCP-слой с v1.7.4)."""
    (knowledge_dir.parent / "outside.md").write_text("# Вне базы\nтайна", encoding="utf-8")
    resp = asyncio.run(web_article(Req(path={"project": "..", "filename": "outside.md"})))
    assert resp.status_code == 404, "traversal через project не отклонён"


def test_web_article_rejects_traversal_via_filename(knowledge_dir):
    (knowledge_dir.parent / "outside.md").write_text("# Вне базы\nтайна", encoding="utf-8")
    resp = asyncio.run(web_article(Req(path={"project": "testproj", "filename": "../../outside.md"})))
    assert resp.status_code == 404, "traversal через filename не отклонён"


def test_web_project_rejects_traversal(knowledge_dir):
    data = _json(asyncio.run(web_project(Req(path={"project": "../.."}))))
    assert data.get("articles") == [], "web_project отдал что-то вне проекта"


def test_web_export_rejects_traversal(knowledge_dir):
    data = _json(asyncio.run(web_export(Req(path={"project": "../.."}))))
    assert data.get("articles") == [], "web_export отдал что-то вне проекта"


def test_web_export_excludes_secrets(knowledge_dir):
    """Экспорт проекта не должен выгружать секреты (даже шифртекст)."""
    proj = knowledge_dir / "testproj"
    (proj / "secret_x.md").write_text("# X\n\n**Секрет:** да\n\nENC:abc\n", encoding="utf-8")
    (proj / "normal.md").write_text("# N\n\nтело\n", encoding="utf-8")
    data = _json(asyncio.run(web_export(Req(path={"project": "testproj"}))))
    names = {a["filename"] for a in data["articles"]}
    assert "secret_x.md" not in names, "секрет попал в экспорт"
    assert "normal.md" in names, "обычная статья пропала из экспорта"


# ─── #2 fail-closed дешифровка ───────────────────────────────────────────────

def _make_secret(knowledge_dir, plaintext):
    from memory_compiler.storage import encrypt_content
    enc = encrypt_content(plaintext)
    assert enc.startswith("ENC:"), "шифрование не сработало — проверь MC_ENCRYPT_KEY в тесте"
    proj = knowledge_dir / "testproj"
    (proj / "secret_db.md").write_text(
        f"# DB\n\n**Секрет:** да\n\n## Содержание\n\n{enc}\n", encoding="utf-8")


def test_web_article_no_decrypt_without_auth(knowledge_dir, monkeypatch):
    """Fail-closed: MC_ENCRYPT_KEY задан, MC_API_KEY пуст (auth не настроен) →
    web_article НЕ отдаёт plaintext секрета (иначе шифрование на диске бессмысленно)."""
    _set_keys(monkeypatch, api_key="", enc_key="test-key-123")
    _make_secret(knowledge_dir, "ПарольProd123")
    resp = asyncio.run(web_article(Req(path={"project": "testproj", "filename": "secret_db.md"})))
    assert "ПарольProd123" not in _json(resp)["content"], "секрет расшифрован без настроенного auth"


def test_web_article_decrypts_with_auth(knowledge_dir, monkeypatch):
    """Контроль: MC_API_KEY задан (эндпоинт под middleware-auth) → секрет дешифруется
    для авторизованного web-пользователя (by design, как было до фикса)."""
    _set_keys(monkeypatch, api_key="apikey-xyz", enc_key="test-key-123")
    _make_secret(knowledge_dir, "ПарольProd123")
    resp = asyncio.run(web_article(Req(path={"project": "testproj", "filename": "secret_db.md"})))
    assert "ПарольProd123" in _json(resp)["content"], "секрет не расшифрован под настроенным auth"


# ─── #11 health без утечки имён проектов ─────────────────────────────────────

def test_web_health_hides_projects_without_auth(knowledge_dir, monkeypatch):
    """Публичный /api/health (Docker healthcheck без токена) не раскрывает имена
    проектов (= имён клиентов). status/version — отдаёт."""
    _set_keys(monkeypatch, api_key="apikey-xyz", enc_key="")
    data = _json(asyncio.run(web_health(Req())))
    assert data["status"] == "ok"
    assert not data.get("projects"), f"имена проектов утекли без auth: {data.get('projects')}"


def test_web_health_full_with_auth_cookie(knowledge_dir, monkeypatch):
    """С валидной кукой (UI после логина) health отдаёт проекты для дашборда."""
    _set_keys(monkeypatch, api_key="apikey-xyz", enc_key="")
    data = _json(asyncio.run(web_health(Req(cookies={"mc_token": "apikey-xyz"}))))
    assert isinstance(data.get("projects"), dict), "под auth health должен отдавать проекты"


def test_web_health_open_when_no_api_key(knowledge_dir, monkeypatch):
    """Если MC_API_KEY не задан — auth нет, health отдаёт полную статистику (как раньше)."""
    _set_keys(monkeypatch, api_key="", enc_key="")
    data = _json(asyncio.run(web_health(Req())))
    assert isinstance(data.get("projects"), dict)


# ─── #10 middleware: ?key= в URL не аутентифицирует ──────────────────────────

def _run_mw(monkeypatch, scope, api_key="secret-key"):
    import memory_compiler.api as api_mod
    monkeypatch.setattr(api_mod, "MC_API_KEY", api_key)
    state = {"passed": False, "responses": []}

    async def app(scope, receive, send):
        state["passed"] = True

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        state["responses"].append(msg)

    mw = AuthMiddleware(app)
    asyncio.run(mw(scope, receive, send))
    return state


def test_auth_middleware_rejects_key_in_query(monkeypatch):
    """?key=... в URL больше не аутентифицирует — ключ утекал в access-логи/Referer."""
    scope = {"type": "http", "path": "/api/export/x",
             "query_string": b"key=secret-key", "headers": []}
    state = _run_mw(monkeypatch, scope)
    assert not state["passed"], "?key= в query всё ещё аутентифицирует"
    assert any(m.get("status") == 401 for m in state["responses"]
               if m.get("type") == "http.response.start")


def test_auth_middleware_accepts_bearer(monkeypatch):
    """Контроль: Bearer-токен аутентифицирует."""
    scope = {"type": "http", "path": "/api/export/x", "query_string": b"",
             "headers": [(b"authorization", b"Bearer secret-key")]}
    state = _run_mw(monkeypatch, scope)
    assert state["passed"], "Bearer-токен должен аутентифицировать"


def test_auth_middleware_accepts_cookie(monkeypatch):
    """Контроль: cookie mc_token аутентифицирует (UI после логина)."""
    scope = {"type": "http", "path": "/api/export/x", "query_string": b"",
             "headers": [(b"cookie", b"mc_token=secret-key")]}
    state = _run_mw(monkeypatch, scope)
    assert state["passed"], "cookie mc_token должна аутентифицировать"


# ─── /sse под авторизацией (КРИТИЧНО из аудита 2026-07-03) ──────────────────
# Раньше /sse и /messages шли МИМО auth: любой, кто дотянется до порта, открывал
# MCP-сессию со всеми tools, включая read_article с расшифровкой секретов.

def test_sse_unauthorized_without_key(monkeypatch):
    scope = {"type": "http", "path": "/sse", "query_string": b"", "headers": []}
    state = _run_mw(monkeypatch, scope)
    assert not state["passed"], "/sse без ключа прошёл мимо auth — дыра из аудита открыта"
    assert any(m.get("status") == 401 for m in state["responses"]
               if m.get("type") == "http.response.start")


def test_sse_rejects_wrong_key(monkeypatch):
    scope = {"type": "http", "path": "/sse",
             "query_string": b"key=wrong-key", "headers": []}
    state = _run_mw(monkeypatch, scope)
    assert not state["passed"], "/sse с неверным ключом аутентифицировался"


def test_sse_accepts_key_in_query(monkeypatch):
    """mcp-remote-клиенты шлют ключ в URL SSE (?key=) — для /sse это принимается
    (осознанный компромисс, REST-путей не касается)."""
    scope = {"type": "http", "path": "/sse",
             "query_string": b"key=secret-key", "headers": []}
    state = _run_mw(monkeypatch, scope)
    assert state["passed"], "?key= в URL /sse должен аутентифицировать"


def test_sse_key_in_query_urldecoded(monkeypatch):
    """Ключ в URL приходит URL-encoded (например $ → %24) — должен декодироваться."""
    scope = {"type": "http", "path": "/sse",
             "query_string": b"key=p%24ss-key", "headers": []}
    state = _run_mw(monkeypatch, scope, api_key="p$ss-key")
    assert state["passed"], "URL-encoded ключ в ?key= не декодирован"


def test_sse_accepts_bearer(monkeypatch):
    scope = {"type": "http", "path": "/sse", "query_string": b"",
             "headers": [(b"authorization", b"Bearer secret-key")]}
    state = _run_mw(monkeypatch, scope)
    assert state["passed"], "Bearer-токен на /sse должен аутентифицировать"


def test_sse_open_when_no_api_key(monkeypatch):
    """MC_API_KEY пуст (dev-режим) — auth выключен целиком, /sse открыт как раньше."""
    scope = {"type": "http", "path": "/sse", "query_string": b"", "headers": []}
    state = _run_mw(monkeypatch, scope, api_key="")
    assert state["passed"]


def test_messages_rides_on_session_id(monkeypatch):
    """/messages/?session_id=... проходит middleware: session_id (uuid4) клиент
    получает только из уже аутентифицированного SSE-стрима, чужой session_id
    отвергает сам транспорт (404)."""
    scope = {"type": "http", "path": "/messages/",
             "query_string": b"session_id=00000000-0000-4000-8000-000000000000",
             "headers": []}
    state = _run_mw(monkeypatch, scope)
    assert state["passed"], "/messages должен ехать на session_id из auth-стрима"


# ─── #1/#2 операционная гигиена ключей ───────────────────────────────────────

def test_key_hygiene_warns_when_api_equals_encrypt(monkeypatch):
    """API-ключ == ключу шифрования: утечка API-ключа (он в каждом запросе)
    раскрывает все секреты. Должно быть предупреждение."""
    import memory_compiler.api as api_mod
    monkeypatch.setattr(api_mod, "MC_API_KEY", "same-key")
    monkeypatch.setattr(api_mod, "MC_ENCRYPT_KEY", "same-key")
    warns = api_mod._check_key_hygiene()
    assert any("шифров" in w.lower() for w in warns), f"нет предупреждения о совпадении ключей: {warns}"


def test_key_hygiene_warns_encrypt_without_api(monkeypatch):
    """MC_ENCRYPT_KEY задан, MC_API_KEY пуст — REST без auth. Предупредить."""
    import memory_compiler.api as api_mod
    monkeypatch.setattr(api_mod, "MC_API_KEY", "")
    monkeypatch.setattr(api_mod, "MC_ENCRYPT_KEY", "enc-key")
    assert api_mod._check_key_hygiene(), "нет предупреждения о шифровании без auth"


def test_key_hygiene_clean_when_distinct(monkeypatch):
    """Разные непустые ключи — никаких предупреждений."""
    import memory_compiler.api as api_mod
    monkeypatch.setattr(api_mod, "MC_API_KEY", "api-key")
    monkeypatch.setattr(api_mod, "MC_ENCRYPT_KEY", "enc-key")
    assert api_mod._check_key_hygiene() == []
