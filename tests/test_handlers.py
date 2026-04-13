"""Tests for handler functions."""
import pytest
from memory_compiler.search import rebuild_index
from memory_compiler.handlers import (
    save_lesson, search, read_article,
    list_projects, add_project, remove_project,
    save_runbook, get_runbook, save_decision,
    list_templates, save_from_template,
    set_project_deps, get_project_deps,
)


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
