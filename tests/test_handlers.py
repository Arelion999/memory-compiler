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
async def test_search(knowledge_dir):
    result = await search("docker", "testproj")
    assert len(result) == 1


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
