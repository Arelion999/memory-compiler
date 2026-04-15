"""Tests for storage module."""
from memory_compiler.storage import (
    auto_tags, extract_git_refs, format_git_refs,
    detect_contradictions, merge_into_article,
    project_dir, today_log_path,
    extract_snippets, extract_errors, TEMPLATES,
    read_project_deps, write_project_deps,
    encrypt_content, decrypt_content, is_encrypted,
    audit_log, read_audit_log,
    parse_git_log_raw, group_commits, format_capture_group,
    html_to_markdown,
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


def test_encrypt_decrypt(monkeypatch):
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "test-passphrase-123")
    text = "Super secret password: P@ssw0rd!"
    encrypted = encrypt_content(text)
    assert encrypted.startswith("ENC:")
    assert "P@ssw0rd" not in encrypted
    decrypted = decrypt_content(encrypted)
    assert decrypted == text


def test_is_encrypted():
    assert is_encrypted("ENC:abc123") is True
    assert is_encrypted("Normal text") is False
    assert is_encrypted("") is False


def test_encrypt_without_key(monkeypatch):
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "")
    result = encrypt_content("test")
    assert result == "test"  # no encryption without key


def test_audit_log(knowledge_dir):
    audit_log("search", {"query": "test", "project": "all"}, 500)
    entries = read_audit_log(10)
    assert len(entries) >= 1
    last = entries[-1]
    assert last["tool"] == "search"
    assert last["size"] == 500


def test_audit_log_hides_content(knowledge_dir):
    audit_log("save_lesson", {"topic": "test", "content": "very long content here"}, 100)
    entries = read_audit_log(10)
    last = entries[-1]
    assert "very long content" not in str(last)
    assert "chars]" in str(last["args"]["content"])


# ─── Git capture ──────────────────────────────────────────────────────────

def test_parse_git_log_raw_basic():
    raw = "abc1234567890|fix: bug|Author|2026-04-01T10:00:00+00:00\n10\t2\tsrc/file.py\n"
    commits = parse_git_log_raw(raw)
    assert len(commits) == 1
    assert commits[0]["hash"] == "abc1234567890"
    assert commits[0]["message"] == "fix: bug"
    assert len(commits[0]["files"]) == 1
    assert commits[0]["files"][0]["insertions"] == 10


def test_parse_git_log_raw_multiple():
    raw = (
        "abc1234567890|feat: new feature|Alice|2026-04-01T10:00:00+00:00\n"
        "50\t0\tsrc/new.py\n"
        "def5678901234|fix: typo|Bob|2026-04-02T11:00:00+00:00\n"
        "1\t1\tREADME.md\n"
    )
    commits = parse_git_log_raw(raw)
    assert len(commits) == 2
    assert commits[0]["author"] == "Alice"
    assert commits[1]["author"] == "Bob"


def test_group_commits_by_prefix():
    commits = [
        {"hash": "a" * 7, "message": "fix: bug", "author": "A", "date": "2026-04-01T00:00:00+00:00", "files": []},
        {"hash": "b" * 7, "message": "feat: thing", "author": "A", "date": "2026-04-02T00:00:00+00:00", "files": []},
        {"hash": "c" * 7, "message": "fix: another", "author": "A", "date": "2026-04-03T00:00:00+00:00", "files": []},
        {"hash": "d" * 7, "message": "random message", "author": "A", "date": "2026-04-04T00:00:00+00:00", "files": []},
    ]
    groups = group_commits(commits, "prefix")
    assert "fix" in groups
    assert "feat" in groups
    assert "other" in groups
    assert len(groups["fix"]) == 2


def test_format_capture_group():
    commits = [
        {"hash": "abc1234", "message": "fix: bug", "author": "A", "date": "2026-04-01T10:00:00+00:00",
         "files": [{"path": "src/a.py", "insertions": 10, "deletions": 2}]},
    ]
    result = format_capture_group("fix", commits)
    assert "fix: bug" in result
    assert "abc1234" in result
    assert "src/a.py" in result


# ─── HTML to markdown ─────────────────────────────────────────────────────

def test_html_to_markdown_basic():
    html = "<h1>Title</h1><p>Hello world</p>"
    md = html_to_markdown(html)
    assert "# Title" in md
    assert "Hello" in md
    assert "world" in md


def test_html_to_markdown_bold():
    html = "<p><strong>bold text</strong></p>"
    md = html_to_markdown(html)
    assert "**" in md
    assert "bold text" in md


def test_html_to_markdown_strips_scripts():
    html = "<p>Keep this</p><script>alert('evil')</script><style>.x{}</style>"
    md = html_to_markdown(html)
    assert "Keep this" in md
    assert "alert" not in md
    assert ".x" not in md


def test_html_to_markdown_lists():
    html = "<ul><li>first</li><li>second</li></ul>"
    md = html_to_markdown(html)
    assert "- first" in md
    assert "- second" in md


# ─── Obsidian parser ──────────────────────────────────────────────────────

def test_parse_obsidian_with_frontmatter():
    from memory_compiler.storage import parse_obsidian_note
    note = """---
title: Test Note
tags:
  - foo
  - bar
---
# Body

Some content with [[Target]] link.
"""
    r = parse_obsidian_note(note)
    assert r["title"] == "Test Note"
    assert "foo" in r["tags"]
    assert "bar" in r["tags"]
    assert "**Target**" in r["body"]
    assert "Target" in r["wiki_links"]


def test_parse_obsidian_inline_tags():
    from memory_compiler.storage import parse_obsidian_note
    note = "# Heading\n\nSome text with #работа and #docker tags"
    r = parse_obsidian_note(note)
    assert "работа" in r["tags"]
    assert "docker" in r["tags"]


def test_parse_obsidian_alias_link():
    from memory_compiler.storage import parse_obsidian_note
    note = "See [[Target Page|display text]] here"
    r = parse_obsidian_note(note)
    assert "**display text**" in r["body"]
    assert "Target Page" in r["wiki_links"]


# ─── Tracking articles ──────────────────────────────────────────────────

def test_tracking_create_and_update(knowledge_dir):
    from memory_compiler.storage import save_tracking_article, load_tracking
    r1 = save_tracking_article("testproj", "release", {"version": "1.0.0"})
    assert r1["action"] == "created"
    assert r1["new_current"]["version"] == "1.0.0"

    # Same version → unchanged
    r2 = save_tracking_article("testproj", "release", {"version": "1.0.0"})
    assert r2["action"] == "unchanged"

    # New version → updated, old goes to history
    r3 = save_tracking_article("testproj", "release", {"version": "1.1.0"})
    assert r3["action"] == "updated"
    assert r3["old_current"]["version"] == "1.0.0"
    assert r3["new_current"]["version"] == "1.1.0"

    data = load_tracking("testproj", "release")
    assert data["current"]["version"] == "1.1.0"
    assert len(data["history"]) == 1
    assert data["history"][0]["version"] == "1.0.0"
    assert data["type"] == "tracking"


def test_tracking_multiple_facts(knowledge_dir):
    from memory_compiler.storage import save_tracking_article, load_tracking
    save_tracking_article("testproj", "deploy", {"host": "10.0.0.1", "port": "8080"})
    save_tracking_article("testproj", "deploy", {"host": "10.0.0.2", "port": "8080"})
    data = load_tracking("testproj", "deploy")
    assert data["current"]["host"] == "10.0.0.2"
    assert len(data["history"]) == 1
    assert data["history"][0]["host"] == "10.0.0.1"


def test_extract_facts(knowledge_dir):
    from memory_compiler.storage import extract_facts_from_text
    # Version + IP in current context
    facts = extract_facts_from_text("Deployed server 10.0.0.5 with version 2.3.1")
    assert "2.3.1" in facts.get("version", [])
    assert "10.0.0.5" in facts.get("ip", [])

    # Historical markers should skip
    facts = extract_facts_from_text("Ранее был IP 192.168.1.1. Переехали со старого 10.0.0.1")
    # Those IPs are in historical sentences — shouldn't appear
    assert "192.168.1.1" not in facts.get("ip", [])
    assert "10.0.0.1" not in facts.get("ip", [])


def test_auto_update_tracking(knowledge_dir):
    from memory_compiler.storage import save_tracking_article, auto_update_tracking, load_tracking
    # Setup: existing tracking with version and host
    save_tracking_article("testproj", "deploy", {"version": "1.0.0", "host": "10.0.0.1"})

    # New text mentions updated values
    updates = auto_update_tracking("testproj", "Выкатили 1.1.0 на 10.0.0.2", "Deploy update")
    assert len(updates) == 1
    data = load_tracking("testproj", "deploy")
    assert data["current"]["version"] == "1.1.0"
    assert data["current"]["host"] == "10.0.0.2"

    # Project without any tracking — no auto-create
    updates2 = auto_update_tracking("otherproj", "Новый сервер 10.0.0.3", "Random note")
    assert len(updates2) == 0


def test_frontmatter_parser(knowledge_dir):
    from memory_compiler.storage import _parse_frontmatter
    text = """---
type: tracking
project: foo
current:
  version: "1.0"
  active: true
history:
  - version: "0.9"
    from: 2026-01-01
---
Body text"""
    data, body = _parse_frontmatter(text)
    assert data["type"] == "tracking"
    assert data["project"] == "foo"
    assert data["current"]["version"] == "1.0"
    assert data["current"]["active"] is True
    assert len(data["history"]) == 1
    assert data["history"][0]["version"] == "0.9"
    assert body.strip() == "Body text"
