"""Tests for storage module."""
from memory_compiler.storage import (
    auto_tags, extract_git_refs, format_git_refs,
    detect_contradictions, merge_into_article,
    project_dir, today_log_path,
    extract_snippets, extract_errors, TEMPLATES,
    read_project_deps, write_project_deps,
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


def test_extract_snippets():
    text = "# Title\n\nSome text\n\n```python\ndef hello():\n    print('hi')\n```\n\nMore text\n\n```bash\necho hello\n```"
    snippets = extract_snippets(text)
    assert len(snippets) == 2
    assert snippets[0]["lang"] == "python"
    assert "def hello" in snippets[0]["code"]
    assert snippets[1]["lang"] == "bash"


def test_extract_snippets_empty():
    snippets = extract_snippets("No code blocks here")
    assert snippets == []


def test_extract_errors():
    text = "Got HTTP error 500 when deploying. Error: ConnectionRefused on port 5432"
    errors = extract_errors(text)
    assert len(errors) > 0
    error_types = [e["type"] for e in errors]
    assert "http_code" in error_types or "error_message" in error_types


def test_extract_errors_empty():
    errors = extract_errors("Everything is fine, no errors")
    assert errors == []


def test_templates_exist():
    assert "bug" in TEMPLATES
    assert "setup" in TEMPLATES
    assert "1c" in TEMPLATES
    assert "deploy" in TEMPLATES
    assert "integration" in TEMPLATES
    for name, tmpl in TEMPLATES.items():
        assert "fields" in tmpl
        assert "format" in tmpl
        assert "description" in tmpl


def test_project_deps(knowledge_dir):
    write_project_deps("testproj", ["general"])
    deps = read_project_deps("testproj")
    assert deps == ["general"]
    # Overwrite
    write_project_deps("testproj", [])
    deps = read_project_deps("testproj")
    assert deps == []


def test_project_deps_nonexistent(knowledge_dir):
    deps = read_project_deps("nonexistent_proj_xyz")
    assert deps == []
