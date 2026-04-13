"""Tests for storage module."""
from memory_compiler.storage import (
    auto_tags, extract_git_refs, format_git_refs,
    detect_contradictions, merge_into_article,
    project_dir, today_log_path,
)


def test_auto_tags_docker():
    tags = auto_tags("Настроил docker-compose для deploy на NAS", "Docker NAS")
    assert "docker" in tags
    assert "deploy" in tags
    assert "nas" in tags


def test_auto_tags_1c():
    tags = auto_tags("Исправил обработку в 1С", "Баг обработки")
    assert "1c" in tags
    assert "bugfix" in tags


def test_extract_git_refs():
    refs = extract_git_refs("Fixed in abc1234def, see #42", "Bugfix")
    assert "commit" in refs
    assert "issue" in refs
    assert "42" in refs["issue"]


def test_format_git_refs():
    refs = {"commit": ["abc1234"], "issue": ["42"]}
    result = format_git_refs(refs)
    assert "Коммиты" in result
    assert "abc1234" in result


def test_detect_contradictions_no_facts(knowledge_dir):
    warnings = detect_contradictions("просто текст без фактов", "testproj")
    assert warnings == []


def test_project_dir_creates(knowledge_dir):
    p = project_dir("newproj")
    assert p.exists()


def test_today_log_path(knowledge_dir):
    p = today_log_path()
    assert p.parent.exists()
    assert p.suffix == ".md"


def test_merge_into_article(knowledge_dir):
    article = knowledge_dir / "testproj" / "test_article.md"
    merge_into_article(article, "New content added", ["newtag"], "2026-04-13 12:00")
    updated = article.read_text(encoding="utf-8")
    assert "New content added" in updated
    assert "newtag" in updated
    assert "2026-04-13 12:00" in updated
