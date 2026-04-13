"""Tests for search module."""
from memory_compiler.search import rebuild_index, _parse_article


def test_parse_article():
    text = "# My Title\n\n**Теги:** docker, test\n\nBody text here"
    result = _parse_article(text, "my_title.md", "testproj")
    assert result["title"] == "My Title"
    assert "docker" in result["tags"]
    assert result["project"] == "testproj"
    assert result["path"] == "testproj/my_title.md"


def test_rebuild_index(knowledge_dir):
    count = rebuild_index()
    assert count >= 1
