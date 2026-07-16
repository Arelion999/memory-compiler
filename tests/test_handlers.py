"""Tests for handler functions."""
import asyncio
import pytest
from memory_compiler.search import rebuild_index
from memory_compiler.handlers import (
    save_lesson, search, read_article,
    list_projects, add_project, remove_project,
    save_runbook, get_runbook, save_decision,
    list_templates, save_from_template,
    set_project_deps, get_project_deps,
    _project_from_cwd, route_project,
)
from memory_compiler.handlers import get_summary


def test_get_summary_skips_frontmatter_and_cleans_tags(knowledge_dir):
    """get_summary: YAML-frontmatter (---) не становится заголовком, а закрывающие
    ** из '**Теги:**' не попадают в теги."""
    proj = knowledge_dir / "testproj"
    (proj / "fm.md").write_text(
        "---\ntype: decision\n---\n# Настоящий заголовок\n"
        "**Теги:** docker, nas\n\n## Записи\n\n### 2026\nтело статьи\n",
        encoding="utf-8",
    )
    res = asyncio.run(get_summary("testproj"))
    text = res[0].text
    assert "**---**" not in text, "frontmatter --- не должен становиться заголовком"
    assert "Настоящий заголовок" in text
    assert "(** " not in text, "закрывающие ** не должны попадать в теги"
    assert "docker, nas" in text


def test_project_from_cwd_match(knowledge_dir):
    import memory_compiler.config as _cfg
    (knowledge_dir / "myapp").mkdir(exist_ok=True)
    (knowledge_dir / "backend").mkdir(exist_ok=True)
    _cfg.PROJECTS = _cfg._discover_projects()
    # Direct match
    assert _project_from_cwd("/home/user/dev/myapp") == "myapp"
    # Nested — most specific (deepest matching) wins
    assert _project_from_cwd("/home/user/dev/myapp/src") == "myapp"
    # Windows paths
    assert _project_from_cwd(r"C:\Users\areli\dev\backend") == "backend"
    # No match
    assert _project_from_cwd("/random/unknown/path") is None
    # Empty
    assert _project_from_cwd("") is None
    assert _project_from_cwd(None) is None


def test_consolidate_finds_similar_articles(knowledge_dir):
    """Two near-duplicate articles should appear in consolidate output."""
    from memory_compiler.handlers import consolidate
    from memory_compiler.search import rebuild_index, rebuild_embeddings
    import memory_compiler.config as _cfg
    import memory_compiler.search as _smod
    proj = knowledge_dir / "myapp"
    proj.mkdir(exist_ok=True)
    (proj / "nginx_a.md").write_text(
        "# Nginx reverse proxy setup\n\n**Теги:** nginx, ssl\n\n"
        "Настроить nginx как обратный прокси. proxy_pass на backend, X-Forwarded-Proto.",
        encoding="utf-8")
    (proj / "nginx_b.md").write_text(
        "# Configure nginx as reverse proxy\n\n**Теги:** nginx, ssl\n\n"
        "Настройка nginx обратного прокси: proxy_pass, заголовок X-Forwarded-Proto.",
        encoding="utf-8")
    (proj / "unrelated.md").write_text(
        "# Recipe for tea\n\nBoil water, add tea leaves.", encoding="utf-8")
    # rebuild_embeddings iterates over search.py's PROJECTS — refresh it
    _cfg.PROJECTS = _cfg._discover_projects()
    _smod.PROJECTS = _cfg.PROJECTS
    rebuild_index()
    rebuild_embeddings()

    result = asyncio.run(consolidate(project="myapp", min_sim=0.6))
    text = result[0].text
    # Both nginx articles should appear as a pair
    assert "nginx_a.md" in text and "nginx_b.md" in text
    # Tea article — not in similarity pairs (too different)
    # (it might still appear if all 3 happen to cluster, but typically not)


def test_consolidate_empty_when_no_embeddings(knowledge_dir):
    from memory_compiler.handlers import consolidate
    from memory_compiler.search import _embeddings
    _embeddings.clear()
    result = asyncio.run(consolidate(project="all"))
    assert "Embeddings" in result[0].text or "нечего сравнивать" in result[0].text


def test_save_compact_creates_and_fifo(knowledge_dir):
    from memory_compiler.handlers import save_compact
    proj = knowledge_dir / "myapp"
    proj.mkdir(exist_ok=True)
    # Save 7 — should keep only 5 (FIFO)
    for i in range(7):
        asyncio.run(save_compact(project="myapp", summary=f"Summary number {i}"))
    cpath = proj / "_compact_history.md"
    assert cpath.exists()
    text = cpath.read_text(encoding="utf-8")
    # Newest (Summary number 6) at top
    assert "Summary number 6" in text
    # FIFO: oldest (Summary number 0, 1) dropped
    assert "Summary number 0" not in text
    assert "Summary number 1" not in text
    # Recent ones kept
    assert "Summary number 2" in text
    assert "Summary number 5" in text


def test_stale_facts_finds_expired_and_expiring(knowledge_dir):
    from memory_compiler.handlers import stale_facts
    import memory_compiler.config as _cfg
    proj = knowledge_dir / "infra"
    proj.mkdir(exist_ok=True)
    # Expired cert
    (proj / "cert_old.md").write_text(
        "# Old SSL cert\n\n**Теги:** ssl\n\nSSL valid until 2024-01-01.", encoding="utf-8")
    # Expiring soon (15 days from now)
    from datetime import datetime, timedelta
    future_date = (datetime.now() + timedelta(days=15)).strftime("%Y-%m-%d")
    (proj / "cert_new.md").write_text(
        f"# New SSL cert\n\n**Теги:** ssl\n\nSSL valid until {future_date}.", encoding="utf-8")
    # Far future
    far = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
    (proj / "cert_far.md").write_text(
        f"# Far SSL cert\n\n**Теги:** ssl\n\nValid until {far}.", encoding="utf-8")
    _cfg.PROJECTS = _cfg._discover_projects()

    result = asyncio.run(stale_facts(project="infra", warn_days=30))
    text = result[0].text
    # Expired
    assert "Old SSL cert" in text
    assert "2024-01-01" in text
    # Expiring (within 30 days)
    assert "New SSL cert" in text
    # Far future — should NOT appear in expiring list (but may show in headers)
    assert "Far SSL cert" not in text


def test_gap_report_empty_audit(knowledge_dir):
    from memory_compiler.handlers import gap_report
    result = asyncio.run(gap_report(project="all", days=30))
    text = result[0].text
    # Should not crash even with no audit data
    assert "Knowledge Gap Report" in text or "Audit-лог пуст" in text


def test_route_project_cwd_override(knowledge_dir):
    import memory_compiler.config as _cfg
    (knowledge_dir / "myapp").mkdir(exist_ok=True)
    _cfg.PROJECTS = _cfg._discover_projects()

    result = asyncio.run(route_project(text="random query about other things",
                                        cwd="/home/user/dev/myapp"))
    text = result[0].text
    # cwd override even when text is unrelated
    assert "myapp" in text
    assert "score: 100" in text
    assert "cwd-match" in text


def test_route_project_deterministic_tie_break(knowledge_dir, monkeypatch):
    """Равные score: тай-брейк по алфавиту (не по порядку listdir — он зависит от ФС
    и давал разный роутинг в разных сессиях → кросс-проектные дубли), а сам случай
    честно помечается «Неоднозначно» вместо молчаливого выбора первого."""
    import memory_compiler.config as _cfg
    # намеренно НЕ-алфавитный порядок в PROJECTS
    monkeypatch.setattr(_cfg, "PROJECTS", ["zeta-app", "alpha-app"])
    out = asyncio.run(route_project(text="миграция данных zeta-app и alpha-app"))[0].text
    candidates = [l for l in out.splitlines() if l.startswith("- **")]
    assert len(candidates) == 2, f"ожидались 2 кандидата: {out}"
    assert candidates[0].startswith("- **alpha-app**"), \
        f"при равном score первым должен идти алфавитно меньший: {candidates}"
    assert "Неоднозначно" in out
    assert 'project="alpha-app"' not in out, "при неоднозначности не должно быть однозначного совета"


def test_route_project_single_strong_candidate(knowledge_dir, monkeypatch):
    """Один сильный кандидат — прежнее поведение: прямой совет project=..."""
    import memory_compiler.config as _cfg
    monkeypatch.setattr(_cfg, "PROJECTS", ["zeta-app", "alpha-app"])
    out = asyncio.run(route_project(text="настроить бэкапы alpha-app"))[0].text
    assert 'project="alpha-app"' in out
    assert "Неоднозначно" not in out


def test_article_history_rejects_traversal(knowledge_dir):
    """LOW из аудита 2026-07-03: article_history строил путь напрямую (без
    safe_article_path) — traversal-зонд существования файлов вне базы."""
    from memory_compiler.handlers import article_history
    (knowledge_dir.parent / "outside.md").write_text("# Вне базы", encoding="utf-8")
    out = asyncio.run(article_history("..", "outside.md"))[0].text
    assert "Небезопасный путь" in out, f"traversal через project не отклонён: {out}"
    out = asyncio.run(article_history("testproj", "../../outside.md"))[0].text
    assert "Небезопасный путь" in out, f"traversal через filename не отклонён: {out}"


@pytest.fixture(autouse=True)
def setup_indexes(knowledge_dir):
    rebuild_index()
    yield


@pytest.mark.asyncio
async def test_save_lesson(knowledge_dir):
    result = await save_lesson("Test Save", "Content for test", "testproj", ["test"])
    assert len(result) == 1
    assert "testproj" in result[0].text


@pytest.mark.asyncio
async def test_save_lesson_force_new_no_overwrite(knowledge_dir):
    """3-е сохранение force_new с тем же topic за день НЕ перезаписывает 2-е:
    раньше проверялся только один запасной путь (slug_YYYYMMDD.md)."""
    for body in ("тело один", "тело два", "тело три"):
        await save_lesson("Коллизия имени", body, "testproj", force_new=True)
    files = sorted(p.name for p in (knowledge_dir / "testproj").glob("*.md")
                   if p.name.startswith("коллизия"))
    assert len(files) == 3, f"ожидалось 3 файла без перезаписи, получено: {files}"


@pytest.mark.asyncio
async def test_search(knowledge_dir):
    result = await search("docker", "testproj")
    # Первый блок — текстовый summary (обратная совместимость)
    assert result[0].type == "text"
    assert "Test Article" in result[0].text
    # За ним — resource links на найденные статьи (memory://<проект>/<файл>)
    links = [b for b in result if getattr(b, "type", None) == "resource_link"]
    assert any(str(l.uri).startswith("memory://testproj/test_article.md") for l in links)


@pytest.mark.asyncio
async def test_search_survives_rerank_failure(knowledge_dir, monkeypatch):
    """Если rerank падает — search отдаёт hybrid-результаты, а не ошибку."""
    import memory_compiler.search as smod

    def _boom(*a, **k):
        raise RuntimeError("reranker exploded")

    monkeypatch.setattr(smod, "rerank", _boom)
    result = await search("docker", "testproj")
    # Мягкая деградация: нашли статью по hybrid, несмотря на упавший rerank
    assert "Test Article" in result[0].text


@pytest.mark.asyncio
async def test_search_survives_rerank_timeout(knowledge_dir, monkeypatch):
    """Если rerank не укладывается в бюджет — best-effort hybrid, без -32001."""
    import time
    import memory_compiler.search as smod
    import memory_compiler.handlers as hmod

    def _slow(query, candidates, top_k=8):
        time.sleep(1.0)
        return candidates[:top_k]

    monkeypatch.setattr(smod, "rerank", _slow)
    monkeypatch.setattr(hmod, "SEARCH_RERANK_BUDGET_S", 0.1)
    result = await search("docker", "testproj")
    assert "Test Article" in result[0].text


@pytest.mark.asyncio
async def test_search_falls_back_to_all_projects(knowledge_dir, monkeypatch):
    """Пусто в узком проекте → авто-фолбэк на project=all с пометкой (#3)."""
    import memory_compiler.search as smod
    monkeypatch.setattr(smod, "PROJECTS", ["testproj", "general"])
    # Изоляция: глобальный _embeddings переживает тесты (conftest сбрасывает только
    # whoosh _ix). Стейл-эмбеддинги прошлых тестов иначе вернут посторонний семантик-хит
    # в testproj и фолбэк не сработает. Чистим → semantic пуст, поиск идёт по BM25.
    smod._embeddings.clear()
    smod._embed_texts.clear()
    # Уникальная сущность лежит в general (без тега shared), ищем из testproj
    (knowledge_dir / "general" / "widget.md").write_text(
        "# Zzyzx настройка\n\n**Дата:** 2026-01-01 10:00\n**Теги:** zzyzx\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nУникальная сущность zzyzx.\n",
        encoding="utf-8",
    )
    smod.rebuild_index()
    result = await search("zzyzx", "testproj")
    text = result[0].text
    assert "Zzyzx" in text, f"фолбэк не нашёл статью в другом проекте: {text}"
    assert "по всем проектам" in text, "нет пометки о кросс-проектном фолбэке"


@pytest.mark.asyncio
async def test_read_article(knowledge_dir):
    result = await read_article("testproj", "test_article.md")
    assert "Test Article" in result[0].text


@pytest.mark.asyncio
async def test_read_article_not_found(knowledge_dir):
    result = await read_article("testproj", "nonexistent.md")
    assert "не найдена" in result[0].text


@pytest.mark.asyncio
async def test_list_projects(knowledge_dir):
    result = await list_projects()
    assert "testproj" in result[0].text


@pytest.mark.asyncio
async def test_add_and_remove_project(knowledge_dir):
    result = await add_project("newtest")
    assert "newtest" in result[0].text
    result = await remove_project("newtest")
    assert "newtest" in result[0].text


@pytest.mark.asyncio
async def test_remove_project_requires_confirm(knowledge_dir):
    """Removing a project with articles requires confirm=True."""
    await add_project("withdata")
    await save_lesson("Test", "content", "withdata")
    # Without confirm — blocked
    result = await remove_project("withdata")
    assert "confirm=True" in result[0].text
    # With confirm — succeeds
    result = await remove_project("withdata", confirm=True)
    assert "withdata" in result[0].text


@pytest.mark.asyncio
async def test_save_runbook(knowledge_dir):
    result = await save_runbook("Deploy Steps", ["Stop service", "Pull code", "Start service"], "testproj")
    assert "Runbook" in result[0].text
    assert "3 шагов" in result[0].text


@pytest.mark.asyncio
async def test_get_runbook(knowledge_dir):
    await save_runbook("Test RB", ["Step 1", "Step 2"], "testproj")
    # Find the file
    import glob
    files = list((knowledge_dir / "testproj").glob("*test_rb*"))
    assert len(files) > 0
    result = await get_runbook("testproj", files[0].name)
    assert "0/2" in result[0].text


@pytest.mark.asyncio
async def test_save_decision(knowledge_dir):
    result = await save_decision(
        "Use PostgreSQL", "PostgreSQL for main DB",
        "MySQL, SQLite", "Better JSON support, extensions",
        "testproj", ["postgres"]
    )
    assert "Решение записано" in result[0].text


@pytest.mark.asyncio
async def test_list_templates():
    result = await list_templates()
    assert "bug" in result[0].text
    assert "setup" in result[0].text


@pytest.mark.asyncio
async def test_save_from_template(knowledge_dir):
    result = await save_from_template(
        "bug",
        {"symptom": "500 error", "cause": "Missing env var", "fix": "Added .env"},
        "testproj"
    )
    assert "testproj" in result[0].text


@pytest.mark.asyncio
async def test_save_from_template_invalid():
    result = await save_from_template("nonexistent", {}, "testproj")
    assert "не найден" in result[0].text


@pytest.mark.asyncio
async def test_set_and_get_project_deps(knowledge_dir):
    result = await set_project_deps("testproj", ["general"])
    assert "general" in result[0].text
    result = await get_project_deps("testproj")
    assert "general" in result[0].text


# ─── Reranker integration in search() and get_context() ────────────────────


def _seed_postgres_articles(knowledge_dir, titles):
    import memory_compiler.config as _cfg
    proj = knowledge_dir / "testproj"
    for i, title in enumerate(titles):
        (proj / f"art_{i}.md").write_text(
            f"# {title}\n\n**Дата:** 2026-01-01 10:00\n"
            f"**Теги:** postgres\n\n## Записи\n\n### 2026-01-01 10:00\n"
            f"{title} — implementation details and configuration body.",
            encoding="utf-8",
        )
    _cfg.PROJECTS = _cfg._discover_projects()
    from memory_compiler.search import rebuild_index, rebuild_embeddings
    rebuild_index()
    rebuild_embeddings()


def test_search_applies_reranker(knowledge_dir, monkeypatch):
    """search() must run results through cross-encoder reranker and surface rerank_score."""
    import memory_compiler.search as search_mod
    titles = ["postgres tuning queries", "postgres backup script", "postgres ssl client cert"]
    _seed_postgres_articles(knowledge_dir, titles)

    class FakeReranker:
        def predict(self, pairs, show_progress_bar=False):
            return [9.0 if "ssl" in t.lower() else 1.0 for _q, t in pairs]

    monkeypatch.setattr(search_mod, "_reranker_model", FakeReranker())

    result = asyncio.run(search("postgres deploy", project="testproj"))
    text = result[0].text
    # Reranker output must be visible (as in start_task) — proves rerank ran
    assert "rerank" in text.lower()
    # SSL article should be first (FakeReranker scored it highest)
    ssl_idx = text.find("ssl client cert")
    tuning_idx = text.find("tuning queries")
    assert ssl_idx > 0, "ssl article not found in output"
    assert tuning_idx < 0 or ssl_idx < tuning_idx, "reranker did not reorder"


def test_search_works_without_reranker(knowledge_dir, monkeypatch):
    """Graceful fallback — when reranker unavailable, search still returns results
    and does NOT expose a rerank score line."""
    import memory_compiler.search as search_mod
    _seed_postgres_articles(knowledge_dir, ["postgres tuning guide"])

    # Marker 'False' = "tried to load and failed" — get_reranker_model returns None
    monkeypatch.setattr(search_mod, "_reranker_model", False)

    result = asyncio.run(search("postgres tuning", project="testproj"))
    text = result[0].text
    assert "postgres tuning guide" in text.lower()
    # No reranker → no rerank score label
    assert "rerank:" not in text.lower()


# ─── lint orphans and dead cross-refs (Karpathy LLM Wiki pattern) ──────────


# ─── Schema.md per-project (Karpathy: project conventions as artifact) ─────


def test_init_schema_creates_file(knowledge_dir):
    """init_schema must create <project>/_schema.md with template sections."""
    import asyncio
    from memory_compiler.handlers import init_schema
    result = asyncio.run(init_schema(project="testproj"))
    text = result[0].text
    assert "testproj" in text
    schema_path = knowledge_dir / "testproj" / "_schema.md"
    assert schema_path.exists()
    schema = schema_path.read_text(encoding="utf-8")
    # Required template sections
    assert "## Сущности" in schema or "## Entities" in schema
    assert "## Связи" in schema or "## Relations" in schema
    assert "## Stylistic" in schema or "## Стиль" in schema


def test_init_schema_idempotent(knowledge_dir):
    """init_schema on a project that already has _schema.md must NOT overwrite it."""
    import asyncio
    from memory_compiler.handlers import init_schema
    proj = knowledge_dir / "testproj"
    custom = "# My custom schema\n\nDo not overwrite!\n"
    (proj / "_schema.md").write_text(custom, encoding="utf-8")
    result = asyncio.run(init_schema(project="testproj"))
    # File preserved
    actual = (proj / "_schema.md").read_text(encoding="utf-8")
    assert actual == custom, "init_schema must not overwrite existing schema"
    # User notified
    text = result[0].text
    assert "already" in text.lower() or "уже" in text.lower() or "exist" in text.lower()


# ─── Cascade-mark on edit_article (Karpathy: stale-reference flagging) ─────


def test_edit_article_marks_dependent_articles(knowledge_dir):
    """When an article is edited, articles that link to it get a 🔄 review marker."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import edit_article
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    (proj / "config.md").write_text(
        "# Config article\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nOriginal config content body line.",
        encoding="utf-8",
    )
    (proj / "deploy.md").write_text(
        "# Deploy article\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nDeploy uses config settings.\n\n"
        "## См. также\n- [Config](./config.md) (2026-01-01)\n",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()

    asyncio.run(edit_article(project="testproj", filename="config.md",
                             content="UPDATED config content with new fields.",
                             append=False))

    deploy_text = (proj / "deploy.md").read_text(encoding="utf-8")
    assert "config.md" in deploy_text  # link still present
    # Review marker injected near the link
    has_marker = "🔄" in deploy_text or "review" in deploy_text.lower() or "обновлен" in deploy_text.lower()
    assert has_marker, f"No cascade-review marker found in deploy.md:\n{deploy_text}"


def test_edit_article_does_not_touch_unrelated(knowledge_dir):
    """Articles that do NOT reference the edited file must remain unchanged."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import edit_article
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    (proj / "target.md").write_text(
        "# Target\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nTarget body content line one.",
        encoding="utf-8",
    )
    (proj / "unrelated.md").write_text(
        "# Unrelated\n\n**Дата:** 2026-01-01 10:00\n**Теги:** other\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nNothing related to the other one body.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()

    before = (proj / "unrelated.md").read_text(encoding="utf-8")
    asyncio.run(edit_article(project="testproj", filename="target.md",
                             content="Updated target body line.", append=False))
    after = (proj / "unrelated.md").read_text(encoding="utf-8")
    assert before == after, "Unrelated article was modified unexpectedly"


# ─── Security: edit_article must NOT break secret encryption (v1.7.23) ──────


def _secret_file(knowledge_dir, project):
    files = list((knowledge_dir / project).glob("secret_*.md"))
    assert len(files) == 1, f"expected 1 secret file, got {files}"
    return files[0].name


def _body_indexed(token):
    """Пути документов, у которых `token` находится в whoosh-поле body.

    Через QueryParser(body), чтобы токен прошёл тот же анализатор, что и при
    индексации (lowercase/stemming) — иначе exact-Term даёт ложные промахи.
    """
    from whoosh.qparser import QueryParser
    from memory_compiler.search import get_index
    ix = get_index()
    with ix.searcher() as s:
        q = QueryParser("body", ix.schema).parse(token)
        return [h["path"] for h in s.search(q, limit=20)]


def test_edit_article_keeps_secret_encrypted(knowledge_dir, monkeypatch):
    """save_secret → edit_article(append=False): шифрование, флаг секрета и
    чистота индекса должны сохраниться. Регрессия High-sev утечки v1.7.21."""
    import memory_compiler.config as cfg
    from memory_compiler.handlers import save_secret, edit_article
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")

    asyncio.run(save_secret(topic="Router creds", project="testproj",
                            content="oldtokenaaa user=root pass=hunter2 ip 10.0.0.1"))
    fname = _secret_file(knowledge_dir, "testproj")

    asyncio.run(edit_article(project="testproj", filename=fname,
                             content="newtokenbbb ssh root@10.0.0.2 pass=qwerty", append=False))

    disk = (knowledge_dir / "testproj" / fname).read_text(encoding="utf-8")
    assert "ENC:" in disk, "тело должно остаться зашифрованным"
    assert "**Секрет:** да" in disk, "флаг секретности потерян"
    assert "newtokenbbb" not in disk, "новый секрет лежит plaintext на диске"
    assert "oldtokenaaa" not in disk, "старый секрет утёк plaintext"

    got = asyncio.run(read_article(project="testproj", filename=fname))[0].text
    assert "newtokenbbb" in got, "read_article должен расшифровать новое тело"

    assert f"testproj/{fname}" not in _body_indexed("newtokenbbb")
    assert f"testproj/{fname}" not in _body_indexed("oldtokenaaa")


def test_save_secret_findable_by_login_not_password(knowledge_dir, monkeypatch):
    """Секрет ищется по логину/IP (теги), но значение пароля НЕ индексируется.

    Регрессия бага ретрива: секрет индексируется только по title/tags, а
    логин лежит в зашифрованном теле → был ненаходим. Фикс: extract_secret_identifiers
    заносит логин/IP в теги. Инвариант: пароль в теги/индекс НЕ попадает."""
    import memory_compiler.config as cfg
    from memory_compiler.handlers import save_secret
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")

    asyncio.run(save_secret(topic="Сервер доступ", project="testproj",
        content="логин svcadmin / пароль Topsecret42xyz. IP 10.9.8.7"))
    fname = _secret_file(knowledge_dir, "testproj")
    disk = (knowledge_dir / "testproj" / fname).read_text(encoding="utf-8")

    tagline = next(l for l in disk.splitlines() if l.lower().startswith("**теги:**"))
    assert "svcadmin" in tagline, "логин не попал в теги — секрет ненаходим"
    assert "10.9.8.7" in tagline, "IP не попал в теги"
    assert "Topsecret42xyz" not in disk, "пароль лежит plaintext на диске"

    res = asyncio.run(search("svcadmin", "testproj"))[0].text
    assert "Сервер доступ" in res, "секрет не находится по логину"
    # значение пароля НЕ должно быть searchable ни по body, ни по tags
    assert f"testproj/{fname}" not in _body_indexed("Topsecret42xyz")


def test_edit_article_secret_append_stays_encrypted(knowledge_dir, monkeypatch):
    """append=True к секрету не должен дописывать plaintext."""
    import memory_compiler.config as cfg
    from memory_compiler.handlers import save_secret, edit_article
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")

    asyncio.run(save_secret(topic="VPN keys", project="testproj",
                            content="basetokenccc wg-key ABCDEF"))
    fname = _secret_file(knowledge_dir, "testproj")

    asyncio.run(edit_article(project="testproj", filename=fname,
                             content="appendtokenddd extra line", append=True))

    disk = (knowledge_dir / "testproj" / fname).read_text(encoding="utf-8")
    assert "appendtokenddd" not in disk, "дописанный секрет лежит plaintext"
    assert "**Секрет:** да" in disk
    assert "ENC:" in disk

    got = asyncio.run(read_article(project="testproj", filename=fname))[0].text
    assert "appendtokenddd" in got, "append-секция должна читаться расшифрованной"
    assert "basetokenccc" in got, "исходный секрет должен остаться читаемым"

    assert f"testproj/{fname}" not in _body_indexed("appendtokenddd")


def test_edit_article_plain_remains_searchable(knowledge_dir, monkeypatch):
    """Несекретная статья после edit_article должна остаться в индексе по телу —
    защита от over-encryption обычных статей."""
    import memory_compiler.config as cfg
    from memory_compiler.handlers import edit_article
    from memory_compiler.search import rebuild_index, rebuild_embeddings
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")

    proj = knowledge_dir / "testproj"
    (proj / "note.md").write_text(
        "# Note\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\noriginal body.\n",
        encoding="utf-8",
    )
    rebuild_index()
    rebuild_embeddings()

    asyncio.run(edit_article(project="testproj", filename="note.md",
                             content="plainsearchtokeneee visible body", append=False))

    disk = (proj / "note.md").read_text(encoding="utf-8")
    assert "ENC:" not in disk, "обычную статью шифровать нельзя"
    assert "plainsearchtokeneee" in disk
    assert "testproj/note.md" in _body_indexed("plainsearchtokeneee"), "несекретная статья пропала из индекса"


def test_edit_article_nonsecret_mentioning_flag_not_encrypted(knowledge_dir, monkeypatch):
    """Баг 1.7.27: обычная статья, цитирующая '**Секрет:** да' в ТЕЛЕ, не должна
    ошибочно шифроваться при edit_article (флаг — только признак меташапки)."""
    import memory_compiler.config as cfg
    from memory_compiler.handlers import edit_article
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")

    proj = knowledge_dir / "testproj"
    (proj / "doc_pro_secrets.md").write_text(
        "# Документация про секреты\n\n"
        "**Дата:** 2026-01-01 10:00\n**Проект:** testproj\n**Теги:** docs, security\n\n"
        "## Записи\n\n"
        "Чтобы пометить статью секретной, save_secret кладёт в шапку `**Секрет:** да`, "
        "а тело шифрует в блок ENC:.\n",
        encoding="utf-8",
    )
    asyncio.run(edit_article(project="testproj", filename="doc_pro_secrets.md",
                             content="docvisibletoken Обновлённая документация о флаге."))

    disk = (proj / "doc_pro_secrets.md").read_text(encoding="utf-8")
    assert "ENC:" not in disk, "документацию ошибочно зашифровало"
    assert "**Секрет:** да" not in disk, "флаг секретности ошибочно добавлен в шапку"
    assert "docvisibletoken" in disk, "тело должно остаться открытым"


def test_merge_into_article_refuses_secret(knowledge_dir, monkeypatch):
    """Защита в глубину: merge_into_article не дописывает plaintext в секрет."""
    import pytest
    import memory_compiler.config as cfg
    from memory_compiler.handlers import save_secret
    from memory_compiler.storage import merge_into_article
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")

    asyncio.run(save_secret(topic="DB creds", project="testproj", content="leaktoken pass=hunter2"))
    f = next((knowledge_dir / "testproj").glob("secret_*.md"))
    before = f.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        merge_into_article(f, "plaintext daily note", ["t"], "2026-01-01 10:00")
    assert f.read_text(encoding="utf-8") == before, "секрет не должен меняться"
    assert "ENC:" in before


def test_find_existing_article_never_targets_secret(knowledge_dir, monkeypatch):
    """Авто-мёрж (save_lesson/compile) не должен выбирать секрет как цель —
    иначе plaintext дописался бы в зашифрованную статью."""
    import memory_compiler.config as cfg
    from memory_compiler.handlers import save_secret
    from memory_compiler.storage import find_existing_article
    from memory_compiler.search import rebuild_index, rebuild_embeddings
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-secret-key-123")

    asyncio.run(save_secret(topic="Mikrotik router access", project="testproj",
                            content="admin password and ip"))
    rebuild_index()
    rebuild_embeddings()

    # запрос с тем же заголовком — без фикса семантика вернула бы секрет
    res = find_existing_article("Mikrotik router access", "admin password and ip", "testproj")
    assert res is None or not res.name.startswith("secret_"), f"авто-мёрж нацелился на секрет: {res}"


def test_reindex_placeholders_secret_files(knowledge_dir):
    """Полный reindex/rebuild_embeddings не должен индексировать тело секрета —
    включая авторские plaintext-секции. Иначе reindex затирает плейсхолдер
    save_secret и контент секрета снова попадает в поиск."""
    from memory_compiler.search import rebuild_index, rebuild_embeddings
    proj = knowledge_dir / "testproj"
    (proj / "secret_mixed.md").write_text(
        "# Mixed secret\n\n**Дата:** 2026-01-01 10:00\n**Теги:** secret\n**Секрет:** да\n\n"
        "## Содержание\n\nENC:gAAAAfakeciphertext\n\n"
        "## Заметки\n\nreindexplainleak плейнтекст-контекст\n",
        encoding="utf-8",
    )
    rebuild_index()
    rebuild_embeddings()
    # плейнтекст-секция секрета НЕ должна быть в индексе после полного reindex
    assert "testproj/secret_mixed.md" not in _body_indexed("reindexplainleak")
    # но статья остаётся findable по заголовку (плейсхолдер проиндексирован)
    assert "testproj/secret_mixed.md" in _body_indexed("mixed")


def test_lint_flags_orphan_article(knowledge_dir):
    """An article not referenced by any other article in the project must be flagged
    with an explicit 'isolated/сирота/no inbound refs' marker."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import lint as lint_handler
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    (proj / "alpha.md").write_text(
        "# Alpha topic\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\n"
        "See also [beta](./beta.md) for context body line.",
        encoding="utf-8",
    )
    (proj / "beta.md").write_text(
        "# Beta topic\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\n"
        "Beta content body line one. Beta content body line two for length.",
        encoding="utf-8",
    )
    (proj / "gamma.md").write_text(
        "# Gamma topic\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\n"
        "Gamma content body line one. Gamma content body line two for length.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()

    result = asyncio.run(lint_handler(project="testproj"))
    text = result[0].text
    # Explicit marker required — find line(s) with the orphan label
    isolated_marker = ("сирота", "isolated", "no inbound")
    lines = text.splitlines()
    isolated_lines = [l for l in lines if any(m in l.lower() for m in isolated_marker)]
    assert isolated_lines, f"No orphan marker line found in output:\n{text}"
    flagged = " ".join(isolated_lines)
    # Gamma is the only article not referenced by anyone
    assert "gamma.md" in flagged
    # Beta is referenced from alpha — must not appear as orphan
    assert "beta.md" not in flagged


def test_lint_flags_dead_cross_reference(knowledge_dir):
    """A markdown link pointing to a missing file must be flagged."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import lint as lint_handler
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    (proj / "broken.md").write_text(
        "# Article with broken link\n\n**Дата:** 2026-01-01 10:00\n**Теги:** docs\n\n"
        "## Записи\n\n### 2026-01-01 10:00\n"
        "Some content. Reference: [missing](./does_not_exist.md) and more text body here.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()

    result = asyncio.run(lint_handler(project="testproj"))
    text = result[0].text
    assert "does_not_exist.md" in text
    assert "dead" in text.lower() or "битая" in text.lower() or "broken" in text.lower()


def test_lint_check2_skips_service_files(knowledge_dir):
    """Service files (_active_context.md, _session.md, _log.md, tracking_*.md)
    intentionally have no yaml header — they must NOT be flagged for missing
    metadata."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import lint as lint_handler
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    # Service files without metadata
    (proj / "_active_context.md").write_text("# Active context\n\nrecent stuff", encoding="utf-8")
    (proj / "_log.md").write_text("# Log\n\n- [...] event", encoding="utf-8")
    (proj / "_reflections.md").write_text("# Reflections\n\n- atomic fact", encoding="utf-8")
    (proj / "tracking_release.md").write_text("---\ncurrent:\n  version: 1.0\n---\n# tracking", encoding="utf-8")
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()
    result = asyncio.run(lint_handler(project="testproj"))
    text = result[0].text
    # None of the service files should be flagged for missing metadata
    metadata_lines = [l for l in text.splitlines() if "нет метаданных" in l]
    flagged = " ".join(metadata_lines)
    assert "_active_context.md" not in flagged
    assert "_log.md" not in flagged
    assert "_reflections.md" not in flagged
    assert "tracking_release.md" not in flagged


def test_lint_fix_removes_dead_refs(knowledge_dir):
    """With fix=true, lint removes dead markdown-link references — keeps the
    link text but drops the broken target."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import lint as lint_handler
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    (proj / "linker.md").write_text(
        "# Linker\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### body\n"
        "Existing: [target](./target.md) and ghost [missing](./ghost.md) and more.",
        encoding="utf-8",
    )
    (proj / "target.md").write_text(
        "# Target\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\nbody.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()
    asyncio.run(lint_handler(project="testproj", fix=True))
    new_text = (proj / "linker.md").read_text(encoding="utf-8")
    # Dead link removed (text kept), existing link preserved
    assert "[target](./target.md)" in new_text  # alive — preserved
    assert "[missing](./ghost.md)" not in new_text  # dead — removed
    assert "missing" in new_text  # link text preserved


def test_lint_orphan_ignores_substring_false_positive(knowledge_dir):
    """An article mentioned only as raw text (not via markdown link) should still
    be flagged orphan if no real link points to it. Current substring-match
    gives false positives — switch to link-parsing detection."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import lint as lint_handler
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    # alpha mentions "target.md" as a raw string in prose (not a link) — should NOT count as ref
    (proj / "alpha.md").write_text(
        "# Alpha\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### body\nДумал про target.md как идею но не ссылка фактически.",
        encoding="utf-8",
    )
    # target — exists but no real link
    (proj / "target.md").write_text(
        "# Target\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### body\nTarget content body line one and two.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()
    result = asyncio.run(lint_handler(project="testproj"))
    text = result[0].text
    orphan_lines = [l for l in text.splitlines()
                    if "сирота" in l.lower() or "isolated" in l.lower() or "no inbound" in l.lower()]
    flagged = " ".join(orphan_lines)
    # target.md mentioned only as plain text → should still be flagged orphan
    assert "target.md" in flagged


def test_mark_dependents_cross_project(knowledge_dir):
    """When article in proja is edited, dependents in projb (linking via
    ../proja/file.md) must also be marked."""
    from memory_compiler.storage import mark_dependents
    import memory_compiler.config as _cfg

    proja = knowledge_dir / "proja"
    projb = knowledge_dir / "projb"
    proja.mkdir(exist_ok=True)
    projb.mkdir(exist_ok=True)
    (proja / "shared.md").write_text("# Shared\n\nbody", encoding="utf-8")
    (projb / "consumer.md").write_text(
        "# Consumer\n\nuses [shared](../proja/shared.md)", encoding="utf-8")
    _cfg.PROJECTS = _cfg._discover_projects()

    count = mark_dependents("proja", "shared.md", "2026-05-18 23:00")
    # consumer.md in projb should be marked
    consumer_text = (projb / "consumer.md").read_text(encoding="utf-8")
    assert "🔄" in consumer_text or "обновлено" in consumer_text
    assert count >= 1


def test_mark_dependents_skips_unreadable_files(knowledge_dir, monkeypatch):
    """mark_dependents must not crash if one of the .md files can't be read.
    Other dependents must still be processed."""
    from memory_compiler.storage import mark_dependents
    proj = knowledge_dir / "testproj"
    (proj / "target.md").write_text(
        "# Target\n\nbody", encoding="utf-8")
    (proj / "linker_ok.md").write_text(
        "# Linker OK\n\nSee [t](./target.md) (existing)", encoding="utf-8")
    # broken.md: simulate via monkeypatching Path.read_text for that name
    bad_path = proj / "broken.md"
    bad_path.write_text("# Broken\n\nSee [t](./target.md) here.", encoding="utf-8")

    orig_read_text = type(bad_path).read_text
    def patched_read_text(self, *a, **kw):
        if self.name == "broken.md":
            raise PermissionError("simulated")
        return orig_read_text(self, *a, **kw)
    monkeypatch.setattr(type(bad_path), "read_text", patched_read_text)

    # Must not raise; should still mark linker_ok.md
    count = mark_dependents("testproj", "target.md", "2026-05-18 22:00")
    assert count >= 1, f"linker_ok.md should be marked; got count={count}"


def test_lint_dead_ref_handles_cross_project_link(knowledge_dir):
    """Cross-project link ../other_proj/file.md should be resolved correctly:
    not flagged when target exists in other project, flagged when missing."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import lint as lint_handler
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj_a = knowledge_dir / "proja"
    proj_b = knowledge_dir / "projb"
    proj_a.mkdir(exist_ok=True)
    proj_b.mkdir(exist_ok=True)
    # Article in projb is the cross-target
    (proj_b / "shared.md").write_text(
        "# Shared\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\nShared content body line.",
        encoding="utf-8",
    )
    # Article in proja with two cross-refs: one existing, one missing
    (proj_a / "linker.md").write_text(
        "# Linker\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "See [shared](../projb/shared.md) and also [ghost](../projb/missing.md) body.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()

    result = asyncio.run(lint_handler(project="proja"))
    text = result[0].text
    dead_lines = [l for l in text.splitlines()
                  if "dead" in l.lower() or "битая" in l.lower() or "broken" in l.lower()]
    flagged = " ".join(dead_lines)
    # missing cross-project ref → flagged
    assert "missing.md" in flagged
    # existing cross-project ref → NOT flagged
    assert "shared.md" not in flagged


def test_lint_dead_ref_handles_cyrillic_filename(knowledge_dir):
    """Cyrillic .md filenames must be matched by the dead-ref regex."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import lint as lint_handler
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    (proj / "linker.md").write_text(
        "# Linker\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "См. [статья](./отсутствующая_статья.md) и тело.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()

    result = asyncio.run(lint_handler(project="testproj"))
    text = result[0].text
    assert "отсутствующая_статья.md" in text


def test_lint_does_not_flag_existing_cross_ref(knowledge_dir):
    """In one project: linker mentions an existing target AND a missing one.
    Lint must flag only the missing reference, not the existing one."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import lint as lint_handler
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    (proj / "target_present.md").write_text(
        "# Target\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nThe target article body has substantial content here.",
        encoding="utf-8",
    )
    (proj / "linker.md").write_text(
        "# Linker\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### 2026-01-01 10:00\n"
        "See [target](./target_present.md) and also [ghost](./ghost_missing.md) body.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()

    result = asyncio.run(lint_handler(project="testproj"))
    text = result[0].text
    dead_lines = [l for l in text.splitlines()
                  if "dead" in l.lower() or "битая" in l.lower() or "broken" in l.lower()]
    flagged = " ".join(dead_lines)
    # Missing target flagged, existing one — not
    assert "ghost_missing.md" in flagged
    assert "target_present.md" not in flagged


def test_get_context_applies_reranker(knowledge_dir, monkeypatch):
    """get_context() with query must also use reranker."""
    import memory_compiler.search as search_mod
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import get_context
    from memory_compiler.search import rebuild_index, rebuild_embeddings

    proj = knowledge_dir / "testproj"
    for i, title in enumerate(["nginx ssl config", "nginx access log rotation", "nginx upstream load balance"]):
        (proj / f"ngx_{i}.md").write_text(
            f"# {title}\n\n**Дата:** 2026-01-01 10:00\n**Теги:** nginx\n\n{title} details body.",
            encoding="utf-8",
        )
    _cfg.PROJECTS = _cfg._discover_projects()
    rebuild_index()
    rebuild_embeddings()

    class FakeReranker:
        def predict(self, pairs, show_progress_bar=False):
            return [9.0 if "upstream" in t.lower() else 1.0 for _q, t in pairs]

    monkeypatch.setattr(search_mod, "_reranker_model", FakeReranker())

    result = asyncio.run(get_context(project="testproj", query="nginx load distribution"))
    text = result[0].text
    # rerank label must surface in output — proves reranker ran on get_context results
    assert "rerank" in text.lower()
    upstream_idx = text.find("upstream load balance")
    ssl_idx = text.find("ssl config")
    assert upstream_idx > 0, "upstream article missing from output"
    assert ssl_idx < 0 or upstream_idx < ssl_idx, "reranker did not reorder in get_context"


@pytest.mark.asyncio
async def test_finish_task_does_not_roll_back_tracking_version(knowledge_dir):
    """Регрессия v1.7.17 (оба трекера): finish_task → save_lesson с release-тегом и
    упоминанием старого git-tag v1.7.14 в content откатывал tracking/release и
    tracking/deployment с 1.7.17 назад на 1.7.14. Авто-пути НЕ должны опускать версию."""
    from memory_compiler.storage import save_tracking_article, load_tracking
    save_tracking_article("testproj", "release", {"version": "1.7.17"})
    save_tracking_article("testproj", "deployment", {"version": "1.7.17"})

    await save_lesson(
        "Деплой релиза на NAS",
        "Рестарт контейнера выполнен. Последний git-tag в репо: v1.7.14.",
        "testproj",
        ["release"],
    )

    assert load_tracking("testproj", "release")["current"]["version"] == "1.7.17", "release откатился"
    assert load_tracking("testproj", "deployment")["current"]["version"] == "1.7.17", "deployment откатился"


def test_compile_after_save_lesson_does_not_duplicate(knowledge_dir):
    """Issue #2 (репро из issue): save_lesson пишет и в статью, и в daily-лог;
    последующий compile НЕ должен мержить ту же запись второй раз и не должен
    ставить ложное «Обновлено»."""
    from memory_compiler.handlers import compile as compile_tool
    asyncio.run(save_lesson(
        topic="тест дубля компиляции",
        content="уникальный текст дубля",
        project="general",
    ))
    asyncio.run(compile_tool(dry_run=False))
    arts = [p for p in (knowledge_dir / "general").glob("*.md")
            if not p.name.startswith("_")]
    assert len(arts) == 1, [a.name for a in arts]
    text = arts[0].read_text(encoding="utf-8")
    assert text.count("уникальный текст дубля") == 1, "запись задвоена compile'ом"
    assert "**Обновлено:**" not in text, "ложное «Обновлено» при дубле"
