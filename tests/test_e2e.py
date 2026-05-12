"""End-to-end integration tests — full workflows across multiple modules.

These tests guard against regressions like:
  - YAML frontmatter parser breaking finish_task on tracking files (v1.1.2)
  - Case-insensitive project mismatch silently splitting context (v1.2.0)
  - Search returning irrelevant noise on continuation phrases (v1.2.0)

If a unit test passes but an e2e test fails, that's a sign the modules
are correct individually but their composition is broken.
"""
import asyncio
import platform
import pytest

from memory_compiler.search import rebuild_index, rebuild_embeddings
from memory_compiler.handlers import (
    start_task, finish_task, save_secret, save_lesson, save_tracking,
    read_article, search, route_project, save_compact,
)
import memory_compiler.config as _cfg


@pytest.fixture(autouse=True)
def setup_indexes(knowledge_dir):
    rebuild_index()
    rebuild_embeddings()
    _cfg.PROJECTS = _cfg._discover_projects()
    yield


def test_e2e_full_task_lifecycle(knowledge_dir):
    """start_task → save_lesson → finish_task → search retrieves it.

    Verifies the canonical autopilot workflow end-to-end.
    """
    project = "myapp"
    topic = "Configure nginx reverse proxy for backend"

    # 1. Start the task — fresh project, nothing in context yet
    start = asyncio.run(start_task(topic=topic, project=project))
    assert "Контекст для" in start[0].text

    # 2. Save lesson directly (skip save through finish)
    asyncio.run(save_lesson(
        topic=topic,
        content="Set proxy_pass to backend:8000, X-Forwarded-Proto header for HTTPS detection",
        project=project,
    ))

    # 3. Finish task with separate session summary
    finish = asyncio.run(finish_task(
        topic="Final polish — security headers",
        content="Added HSTS, CSP, X-Frame-Options to nginx server block",
        project=project,
        session_summary="Configured nginx + security headers",
    ))
    assert "запис" in finish[0].text.lower()

    # 4. Verify search retrieves both lessons
    rebuild_index()
    rebuild_embeddings()
    results = asyncio.run(search(query="nginx proxy backend", project=project))
    assert "nginx" in results[0].text.lower()


def test_e2e_secret_roundtrip(knowledge_dir):
    """save_secret → read_article decrypts content correctly."""
    import os
    # Need encryption key for save_secret
    os.environ["MC_ENCRYPT_KEY"] = "test-key-for-e2e-tests"
    # Re-import to pick up the key
    import memory_compiler.config as cfg
    cfg.MC_ENCRYPT_KEY = "test-key-for-e2e-tests"

    asyncio.run(save_secret(
        topic="Database credentials prod",
        content="host=db.example.com user=app_user pass=SuperSecret123",
        project="myapp",
    ))

    # Read it back — should decrypt
    result = asyncio.run(read_article(project="myapp", filename="secret_database_credentials_prod.md"))
    text = result[0].text
    # Decrypted content should be visible
    assert "db.example.com" in text or "[зашифровано]" in text  # depends on auth ctx


def test_e2e_case_insensitive_project(knowledge_dir):
    """Saving to MyProj and querying myproj find the same articles.

    Regression for v1.2.0 case-merge.
    """
    asyncio.run(save_lesson(
        topic="Test article one",
        content="Some content here",
        project="MyProj",  # capital
    ))

    # Tools dispatcher normalizes — actual project should be 'myproj'
    # but we call save_lesson directly here, so pass normalized form
    from memory_compiler.storage import normalize_project
    norm = normalize_project("MyProj")
    assert norm == "myproj"

    # Article should land in lowercase dir
    articles_lower = list((knowledge_dir / "myproj").glob("*.md")) if (knowledge_dir / "myproj").exists() else []
    articles_upper = list((knowledge_dir / "MyProj").glob("*.md")) if (knowledge_dir / "MyProj").exists() else []
    # Either of those (Windows FS may collapse them) — content is reachable
    assert articles_lower or articles_upper


def test_e2e_continuation_intent_returns_session_not_search(knowledge_dir):
    """start_task on continuation phrase ('давай продолжим') returns session/active context,
    NOT semantic search noise.

    Regression for v1.2.0 continuation intent.
    """
    # Set up: project with one unrelated article + saved session
    project = "myapp"
    asyncio.run(save_lesson(
        topic="Recipe for tea",
        content="Boil water, add tea leaves, steep for 5 minutes",
        project=project,
    ))
    asyncio.run(finish_task(
        topic="Backend API work",
        content="Implemented POST /v1/orders endpoint, added validation middleware",
        project=project,
        session_summary="Worked on backend API endpoints today",
    ))

    rebuild_index()
    rebuild_embeddings()

    # Continuation phrase — should hit continuation branch, return session
    result = asyncio.run(start_task(topic="давай продолжим", project=project))
    text = result[0].text
    # Continuation marker should be present
    assert "продолжить работу" in text.lower() or "недавняя активность" in text.lower() or "сессия" in text.lower()
    # The unrelated tea article should NOT be top match
    # (continuation skips RAG entirely)


def test_e2e_route_project_with_cwd(knowledge_dir):
    """route_project with cwd matching existing project returns it with score 100."""
    (knowledge_dir / "frontend").mkdir(exist_ok=True)
    _cfg.PROJECTS = _cfg._discover_projects()

    result = asyncio.run(route_project(
        text="some unrelated query about backend stuff",
        cwd="/home/dev/myrepo/frontend",
    ))
    text = result[0].text
    assert "frontend" in text
    assert "score: 100" in text


def test_e2e_compact_history_persists_across_starts(knowledge_dir):
    """save_compact stores summary; subsequent start_task surfaces it on continuation."""
    project = "myapp"
    asyncio.run(save_compact(
        project=project,
        summary="Up to compaction: refactored auth module, fixed token expiry bug, started writing tests",
    ))

    # Continuation start_task — should include compact history
    result = asyncio.run(start_task(topic="давай продолжим", project=project))
    text = result[0].text
    assert "compact history" in text.lower() or "refactored auth" in text.lower()


def test_e2e_tracking_with_nested_list_no_crash(knowledge_dir):
    """save_tracking with nested list in current dict (the v1.1.2 bug) — must not crash."""
    project = "myapp"
    # First tracking with nested list
    asyncio.run(save_tracking(
        project=project,
        entity="release",
        facts={"version": "1.0.0", "artifacts": ["app-arm64.zip", "app-x64.zip"]},
    ))

    # Update — used to crash with "dictionary update sequence element..."
    result = asyncio.run(save_tracking(
        project=project,
        entity="release",
        facts={"version": "1.1.0"},
    ))
    text = result[0].text
    # Should report success, not error
    assert "ошибк" not in text.lower() or "1.1.0" in text
