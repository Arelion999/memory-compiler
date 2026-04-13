"""Tests for handler functions."""
import pytest
from memory_compiler.search import rebuild_index
from memory_compiler.handlers import (
    save_lesson, search, read_article,
    list_projects, add_project, remove_project,
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
