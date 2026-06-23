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


def test_detect_contradictions_different_subnets_no_warning(knowledge_dir):
    """IP в разных /24 подсетях — не конфликт (разные сервера)."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "nas.md").write_text(
        "NAS Synology IP: 192.168.88.100", encoding="utf-8"
    )
    # Новая запись про 1С сервер в другой подсети
    warnings = detect_contradictions(
        "Сервер 1С на 192.168.1.55 (PROD)", "testproj"
    )
    assert warnings == []


def test_detect_contradictions_same_subnet_different_entity_no_warning(knowledge_dir):
    """Одна подсеть, но разные сущности — не конфликт."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "nginx.md").write_text(
        "Nginx reverse proxy на 192.168.1.10", encoding="utf-8"
    )
    warnings = detect_contradictions(
        "Postgres database на 192.168.1.20", "testproj"
    )
    assert warnings == []


def test_detect_contradictions_same_entity_same_subnet_warning(knowledge_dir):
    """Одна сущность, одна подсеть, разные IP — реальный конфликт (миграция)."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "nginx.md").write_text(
        "Nginx сервер на 192.168.1.10", encoding="utf-8"
    )
    warnings = detect_contradictions(
        "Nginx переехал на 192.168.1.50", "testproj"
    )
    assert len(warnings) > 0
    assert "192.168.1.10" in warnings[0]
    assert "192.168.1.50" in warnings[0]


def test_detect_contradictions_same_entity_cross_subnet_warning(knowledge_dir):
    """Одна сущность, РАЗНЫЕ подсети — ВАЖНОЕ предупреждение (переезд в другую сеть)."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "nginx_old.md").write_text(
        "Nginx reverse proxy был на 10.0.5.20", encoding="utf-8"
    )
    # Тот же nginx, переехал в другую сеть
    warnings = detect_contradictions(
        "Nginx теперь на 192.168.1.100", "testproj"
    )
    assert len(warnings) > 0, "Переезд nginx в другую подсеть должен вызвать предупреждение"


def test_detect_contradictions_private_vs_public_ip_no_warning(knowledge_dir):
    """LAN-адрес (RFC1918) и WAN (публичный) — разные роли, не конфликт даже при общем теге.
    93.184.216.34 — публичный IP example.com (безопасный для тестов).
    """
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "router_alpha.md").write_text(
        "Router alpha: PPPoE WAN внешний IP 93.184.216.34", encoding="utf-8"
    )
    warnings = detect_contradictions(
        "Router beta: LAN-интерфейс 192.168.50.1", "testproj"
    )
    assert warnings == [], f"Private vs public IP не должен давать FP, получено: {warnings}"


def test_detect_contradictions_well_known_public_dns_no_warning(knowledge_dir):
    """Известные публичные адреса (8.8.8.8, 1.1.1.1) не сравниваются ни с чем."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "router_doh.md").write_text(
        "Router: DoH резолвер 8.8.8.8 (Google DNS)", encoding="utf-8"
    )
    warnings = detect_contradictions(
        "Router: новая LAN-подсеть 192.168.50.0", "testproj"
    )
    assert warnings == [], f"Well-known DNS не должен давать FP, получено: {warnings}"


def test_detect_contradictions_different_hostnames_in_filename_no_warning(knowledge_dir):
    """Разные имена устройств в filename → разные физические экземпляры, не конфликт."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "router_alpha__wan_drop.md").write_text(
        "Router alpha: WAN-канал 93.184.216.34 потери", encoding="utf-8"
    )
    (proj / "router_beta__doh.md").write_text(
        "Router beta: DoH 8.8.8.8 захлёбывался", encoding="utf-8"
    )
    # Третий роутер — офисный, новая статья
    warnings = detect_contradictions(
        "Router gamma: LAN-интерфейс 192.168.50.1", "testproj"
    )
    assert warnings == [], f"Разные hostname в filename → разные устройства, получено: {warnings}"


def test_detect_contradictions_cidr_network_address_no_warning(knowledge_dir):
    """CIDR-подсеть (192.168.50.0/24) не сравнивается с хостом в этой же подсети."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "lan_subnet.md").write_text(
        "Офисная LAN: 192.168.50.0/24", encoding="utf-8"
    )
    warnings = detect_contradictions(
        "Router LAN: шлюз 192.168.50.1", "testproj"
    )
    assert warnings == [], f"CIDR network-address ≠ host, получено: {warnings}"


def test_detect_contradictions_mixed_roles_no_warning(knowledge_dir):
    """Регрессия: смесь ролей IP (WAN public + DNS public + LAN private + tunnel private)
    в статьях про устройства одного класса не должна давать FP.
    """
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "router_alpha__wan_drop.md").write_text(
        "Router alpha: WAN PPPoE 93.184.216.34 firewall drop в логе", encoding="utf-8"
    )
    (proj / "router_beta__doh.md").write_text(
        "Router beta: DoH резолвер 8.8.8.8 захлёбывался", encoding="utf-8"
    )
    (proj / "router_alpha__download_low.md").write_text(
        "Router alpha: download упал, WAN IP 93.184.216.34", encoding="utf-8"
    )
    # Новая статья — устранение конфликта LAN-подсети
    new = ("Router gamma: LAN сменил с 192.168.1.0/24 "
           "на 192.168.50.0/24, шлюз 192.168.50.1, DMZ via VPN 10.10.250.1")
    warnings = detect_contradictions(new, "testproj")
    assert warnings == [], f"Смесь ролей IP не должна давать FP, получено: {warnings}"


def test_detect_contradictions_versions_no_warning(knowledge_dir):
    """Версии монотонно растут во времени: разные версии одного сервиса в
    разных статьях — это эволюция, а не противоречие. Текущую версию ведёт
    tracking (save_tracking/get_current). Даже общая сущность (nginx) не должна
    превращать разницу версий в ложный конфликт.
    """
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "nginx_deploy_old.md").write_text(
        "Nginx сервис: версия 1.1.0 на проде", encoding="utf-8"
    )
    # Тот же nginx, версия выросла за месяцы работы
    warnings = detect_contradictions(
        "Nginx обновлён: версия 1.7.8", "testproj"
    )
    assert warnings == [], f"Разные версии — эволюция, не конфликт, получено: {warnings}"


def test_detect_contradictions_four_part_version_not_ip(knowledge_dir):
    """4-частная версия (0.2.0.5 расширения 1С) не должна распознаваться как IP
    (0.0.0.0/8 — не хост) и конфликтовать с реальным адресом сервера."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "config.md").write_text(
        "docker postgres, сервер 1С 192.168.1.55", encoding="utf-8"
    )
    warnings = detect_contradictions(
        "docker: версия расширения 0.2.0.4 → 0.2.0.5", "testproj"
    )
    assert warnings == [], f"4-частная версия не должна сравниваться как IP: {warnings}"


def test_detect_contradictions_invalid_octet_version_not_ip(knowledge_dir):
    """Версия-сборка с октетом >255 (1.2.3.300) ловится IP-regex, но не валидна
    как IP (L1) → не должна сравниваться с реальным адресом сервера."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "config.md").write_text(
        "docker postgres, сервер 192.168.1.55", encoding="utf-8"
    )
    warnings = detect_contradictions(
        "docker: обновили сборку до 1.2.3.300", "testproj"
    )
    assert warnings == [], f"Невалидный октет (версия-сборка) не должен сравниваться как IP: {warnings}"


def test_extract_facts_url_normalized():
    """URL нормализуется (L4): хвостовая пунктуация срезается, query/fragment
    (там же ?key=СЕКРЕТ) отбрасывается, схема+хост в нижний регистр, хвостовой /
    убран. Идентичный эндпоинт перестаёт быть «разными строками» и не течёт
    ключом в факты/tracking."""
    from memory_compiler.storage import _extract_facts
    facts = _extract_facts('config: "http://Host.LAN:8765/SSE/?key=fake%24",')
    assert facts.get("URL") == {"http://host.lan:8765/SSE"}


def test_detect_contradictions_url_query_difference_no_warning(knowledge_dir):
    """Один эндпоинт, отличается только query (?key=) — не противоречие (L4)."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "ep_old.md").write_text(
        "Endpoint: http://10.20.30.40:8765/sse?key=AAA", encoding="utf-8"
    )
    warnings = detect_contradictions(
        "Тот же endpoint, другой ключ: http://10.20.30.40:8765/sse?key=BBB", "testproj"
    )
    assert warnings == [], f"Разница только в query не должна давать FP: {warnings}"


def test_detect_contradictions_url_trailing_punctuation_no_warning(knowledge_dir):
    """Идентичный URL не должен давать ложное противоречие из-за хвоста:
    в старой статье он записан в JSON-виде («url",), в новой — чисто.
    """
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "old_endpoint.md").write_text(
        'Endpoint в конфиге: "http://10.20.30.40:8765/sse",', encoding="utf-8"
    )
    warnings = detect_contradictions(
        "Тот же endpoint: http://10.20.30.40:8765/sse", "testproj"
    )
    assert warnings == [], f"Идентичный URL не должен давать FP, получено: {warnings}"


# ─── update_cross_references: защита от загрязнения (B+C+D) ───────────────────

def _vec(sim):
    """2D единичный вектор, чей dot с [1,0] равен sim."""
    import math
    return [sim, math.sqrt(max(0.0, 1.0 - sim * sim))]


def _xref_setup(monkeypatch, q_vec, embeddings):
    """Инъекция stub-модели и контролируемого _embeddings в search-модуль."""
    import numpy as np
    import memory_compiler.search as search_mod

    class _Stub:
        def encode(self, texts, normalize_embeddings=True, **kw):
            return np.array([q_vec], dtype=float)

    monkeypatch.setattr(search_mod, "get_embed_model", lambda: _Stub())
    monkeypatch.setattr(
        search_mod, "_embeddings",
        {k: np.array(v, dtype=float) for k, v in embeddings.items()},
    )


def test_update_cross_references_scoped_and_windowed(knowledge_dir, monkeypatch):
    """Кросс-реф только в ТОТ ЖЕ проект и только в окне similarity:
    чужой проект, слишком далёкие и слишком близкие (дубль) — не линкуются.
    """
    from memory_compiler.storage import update_cross_references
    kb = knowledge_dir
    (kb / "testproj" / "in_window.md").write_text("про docker", encoding="utf-8")
    (kb / "testproj" / "too_far.md").write_text("про nginx", encoding="utf-8")
    (kb / "testproj" / "too_close.md").write_text("дубль", encoding="utf-8")
    (kb / "otherproj").mkdir(exist_ok=True)
    (kb / "otherproj" / "cross.md").write_text("чужой проект", encoding="utf-8")
    _xref_setup(monkeypatch, _vec(1.0), {
        "testproj/in_window.md": _vec(0.88),
        "testproj/too_far.md": _vec(0.50),
        "testproj/too_close.md": _vec(0.99),
        "otherproj/cross.md": _vec(0.90),
    })
    update_cross_references("Новая тема", "testproj", "testproj/new.md")
    assert "См. также" in (kb / "testproj" / "in_window.md").read_text(encoding="utf-8")
    assert "См. также" not in (kb / "testproj" / "too_far.md").read_text(encoding="utf-8")
    assert "См. также" not in (kb / "testproj" / "too_close.md").read_text(encoding="utf-8")
    assert "См. также" not in (kb / "otherproj" / "cross.md").read_text(encoding="utf-8"), \
        "кросс-проектный линк не должен создаваться"


def test_update_cross_references_caps_total(knowledge_dir, monkeypatch):
    """Потолок: при многих подходящих кандидатах линкуются только top-N (5)."""
    from memory_compiler.storage import update_cross_references
    kb = knowledge_dir
    sims = {"a": 0.96, "b": 0.94, "c": 0.92, "d": 0.90, "e": 0.88, "f": 0.86}
    emb = {}
    for name, s in sims.items():
        (kb / "testproj" / f"{name}.md").write_text(f"article {name}", encoding="utf-8")
        emb[f"testproj/{name}.md"] = _vec(s)
    _xref_setup(monkeypatch, _vec(1.0), emb)
    update_cross_references("Тема", "testproj", "testproj/new.md")
    linked = [n for n in sims
              if "См. также" in (kb / "testproj" / f"{n}.md").read_text(encoding="utf-8")]
    assert len(linked) == 5, f"ожидался потолок top-5, получено: {linked}"
    assert "f" not in linked, "должен отсекаться кандидат с наименьшим sim"


def test_update_cross_references_skips_meta_source(knowledge_dir, monkeypatch):
    """Meta-статья (health-check/session) не должна кросс-реферить ничего —
    они семантически близки ко всему и засевали базу нерелевантными ссылками.
    """
    from memory_compiler.storage import update_cross_references
    kb = knowledge_dir
    (kb / "testproj" / "normal.md").write_text("обычная статья", encoding="utf-8")
    _xref_setup(monkeypatch, _vec(1.0), {"testproj/normal.md": _vec(0.80)})
    update_cross_references("Health-check", "testproj", "testproj/health-check_2026-06-09.md")
    assert "См. также" not in (kb / "testproj" / "normal.md").read_text(encoding="utf-8")


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


def test_auto_update_tracking_picks_max_version(knowledge_dir):
    """auto_update_tracking берёт МАКСИМАЛЬНУЮ версию из текста, не первую —
    иначе перечисление 1.7.11…1.7.16 откатывало трекер на 1.7.11.
    (Заметка ссылается на сущность 'release' — этого требует relevance-гейт.)"""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking, load_tracking
    save_tracking_article("testproj", "release", {"version": "1.7.10"})
    auto_update_tracking("testproj", "release: v1.7.11, затем v1.7.12, финал version 1.7.16", "Releases")
    data = load_tracking("testproj", "release")
    assert data["current"]["version"] == "1.7.16", f"ожидался max 1.7.16, got {data['current']}"


def test_auto_update_tracking_no_version_regression(knowledge_dir):
    """Регрессия v1.7.17: автоматический скан НЕ должен откатывать версию трекера
    назад. Заметка ссылается на 'deployment' (проходит relevance-гейт) и упоминает
    старый git-tag v1.7.14, трекер на 1.7.17 → должен остаться 1.7.17."""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking, load_tracking
    save_tracking_article("testproj", "deployment", {"version": "1.7.17"})
    updates = auto_update_tracking("testproj", "deployment: рестарт выполнен, последний git-tag v1.7.14", "Deploy")
    data = load_tracking("testproj", "deployment")
    assert data["current"]["version"] == "1.7.17", f"трекер откатился назад: {data['current']}"
    assert updates == [], f"отката быть не должно, а есть апдейт: {updates}"


def test_save_tracking_article_guards_version_regression(knowledge_dir):
    """save_tracking_article(guard_version_regression=True) не опускает версию ниже
    текущей; движение вперёд при этом работает как обычно."""
    from memory_compiler.storage import save_tracking_article, load_tracking
    save_tracking_article("testproj", "release", {"version": "1.7.17"})
    # Откат назад заблокирован → unchanged
    r = save_tracking_article("testproj", "release", {"version": "1.7.14"}, guard_version_regression=True)
    assert r["action"] == "unchanged", f"откат не заблокирован: {r}"
    assert load_tracking("testproj", "release")["current"]["version"] == "1.7.17"
    # Движение вперёд по-прежнему работает
    r2 = save_tracking_article("testproj", "release", {"version": "1.7.18"}, guard_version_regression=True)
    assert r2["action"] == "updated"
    assert load_tracking("testproj", "release")["current"]["version"] == "1.7.18"


def test_save_tracking_article_explicit_downgrade_still_allowed(knowledge_dir):
    """Без guard (явный вызов save_tracking) откат версии РАЗРЕШЁН — реальные
    production-rollback'и должны фиксироваться. Защищаем только авто-пути."""
    from memory_compiler.storage import save_tracking_article, load_tracking
    save_tracking_article("testproj", "release", {"version": "1.7.17"})
    r = save_tracking_article("testproj", "release", {"version": "1.7.14"})
    assert r["action"] == "updated"
    assert load_tracking("testproj", "release")["current"]["version"] == "1.7.14"


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


def test_parse_frontmatter_nested_list_in_nested_dict():
    """Regression: list inside nested dict must NOT overwrite the nested dict."""
    from memory_compiler.storage import _parse_frontmatter
    text = """---
type: tracking
project: myproj
entity: release
current:
  version: 0.29.1
  deployed_to_prod: true
  apk_files:
    - myapp-0.29.1-arm64-v8a.apk
    - myapp-0.29.1-armeabi-v7a.apk
history: []
---
body text
"""
    data, body = _parse_frontmatter(text)
    assert isinstance(data["current"], dict), "current must remain a dict"
    assert data["current"]["version"] == "0.29.1"
    assert data["current"]["deployed_to_prod"] is True
    assert data["current"]["apk_files"] == [
        "myapp-0.29.1-arm64-v8a.apk",
        "myapp-0.29.1-armeabi-v7a.apk",
    ]
    assert data["history"] == []
    assert body.strip() == "body text"


def test_save_tracking_survives_corrupted_current(knowledge_dir):
    """If existing tracking file has corrupted current (not a dict),
    save_tracking_article must not crash — it regenerates fresh."""
    from memory_compiler.storage import save_tracking_article, load_tracking
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    # Simulate a file corrupted by legacy parser (current is a list of strings)
    corrupted = """---
type: tracking
project: testproj
entity: release
current:
  - file1.apk
  - file2.apk
history: []
---
"""
    (proj / "tracking_release.md").write_text(corrupted, encoding="utf-8")
    # With pyyaml current will be a list; this must not crash save.
    result = save_tracking_article("testproj", "release", {"version": "1.0.0"})
    assert result["action"] in ("created", "updated")
    data = load_tracking("testproj", "release")
    assert data["current"]["version"] == "1.0.0"


def test_normalize_project_basic():
    from memory_compiler.storage import normalize_project
    assert normalize_project("MyProj_X") == "myproj_x"
    assert normalize_project("  Backend_Service  ") == "backend_service"
    assert normalize_project("infra") == "infra"
    assert normalize_project("") == "general"
    assert normalize_project(None) == "general"


def test_merge_case_duplicates(knowledge_dir):
    """Migration: rename uppercase dir to lowercase if no lowercase exists,
    or merge files into lowercase if both exist (Linux only — Windows FS is case-insensitive)."""
    import platform, sys
    if platform.system() == "Windows":
        # Windows NTFS treats UPPER and lower as same dir — case-merge can't be tested locally.
        # Skip; the migration code is exercised on Linux container in production.
        return
    from memory_compiler.storage import merge_case_duplicates
    upper = knowledge_dir / "UPPERPROJ"
    upper.mkdir(exist_ok=True)
    (upper / "article.md").write_text("uppercase content", encoding="utf-8")

    merges = merge_case_duplicates()
    # Renamed/merged into lowercase
    assert any(m["from"] == "UPPERPROJ" and m["to"] == "upperproj" for m in merges)
    lower = knowledge_dir / "upperproj"
    assert lower.exists()
    assert (lower / "article.md").exists()
    assert not upper.exists()


def test_list_tracking_skips_corrupted_current(knowledge_dir):
    """list_tracking_articles must skip entries where current is not a dict."""
    from memory_compiler.storage import list_tracking_articles, save_tracking_article
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    # Create one valid tracking
    save_tracking_article("testproj", "good", {"version": "1.0.0"})
    # Create corrupted tracking file
    corrupted = """---
type: tracking
project: testproj
entity: bad
current:
  - just
  - list
history: []
---
"""
    (proj / "tracking_bad.md").write_text(corrupted, encoding="utf-8")
    # list_tracking_articles should return only good one
    listed = list_tracking_articles("testproj")
    entities = [t["entity"] for t in listed]
    assert "good" in entities
    assert "bad" not in entities


# ─── Per-project _log.md (Karpathy LLM Wiki pattern) ────────────────────────


def test_version_regex_does_not_match_ip_octets():
    """Version regex must NOT capture the first 3 octets of an IPv4 address.
    `\\d+\\.\\d+\\.\\d+` was greedy enough to match '51.79.124' inside IP
    '80.81.82.83', poisoning auto_update_tracking for version fields."""
    from memory_compiler.storage import _FACT_PATTERNS
    text = "Attacker C2 server at 80.81.82.83 was spotted."
    matches = _FACT_PATTERNS["version"].findall(text)
    assert matches == [], f"Version regex must not match IP octets, got: {matches}"


def test_auto_update_tracking_only_strict_key_match(knowledge_dir):
    """auto_update_tracking must use strict key names, not substring match.
    Otherwise keys like 'iptables_policy', 'hosting', 'bitrix_version_date'
    accidentally hit the ip/host/version dispatcher and get overwritten."""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking
    import memory_compiler.config as _cfg

    # Set up a tracking article with mixed keys that contain ip/host/version
    # as substrings but are NOT IP/host/version fields.
    save_tracking_article("testproj", "deployment", {
        "host": "example.ru",            # legitimate host
        "ip": "80.80.80.80",                # legitimate IP
        "hosting": "Khabarovsk DC",          # NOT an IP — substring "host" caught it before
        "iptables_policy": "ACCEPT",         # NOT an IP — substring "ip" caught it
        "bitrix_version": "25.100.200",      # legitimate version
        "bitrix_version_date": "2025-03-18", # NOT a version — substring "version" caught it
    })
    _cfg.PROJECTS = _cfg._discover_projects()

    # Lesson text mentions an ATTACKER's IP — must NOT overwrite our deployment.
    text = "Found webshells. C2 server at 80.81.82.83 (OVH Canada) and SEO spam."
    updates = auto_update_tracking("testproj", text, topic="incident report")

    # Re-load tracking, verify nothing wrong got overwritten
    from memory_compiler.storage import load_tracking
    data = load_tracking("testproj", "deployment")
    cur = data["current"]
    # Non-IP/host/version fields must stay intact
    assert cur["hosting"] == "Khabarovsk DC", f"hosting got overwritten: {cur['hosting']!r}"
    assert cur["iptables_policy"] == "ACCEPT", f"iptables_policy overwritten: {cur['iptables_policy']!r}"
    # YAML parses ISO dates to datetime.date — accept either str or date object
    bvd = cur["bitrix_version_date"]
    assert str(bvd) == "2025-03-18", \
        f"bitrix_version_date overwritten: {bvd!r}"
    # bitrix_version must stay since the attacker IP is not a real version
    assert cur["bitrix_version"] == "25.100.200", \
        f"bitrix_version overwritten by IP octets: {cur['bitrix_version']!r}"
    # Relevance-гейт: заметка-инцидент не ссылается на 'deployment' → даже легитимные
    # ip/host НЕ должны затираться чужим (атакующим) IP из текста.
    assert cur["ip"] == "80.80.80.80", f"реальный IP затёрт IP атакующего: {cur['ip']!r}"
    assert cur["host"] == "example.ru", f"host затёрт: {cur['host']!r}"


def test_safe_path_rejects_dot_project(knowledge_dir):
    """project='.' must be rejected — it resolves to KNOWLEDGE_DIR itself,
    allowing access to root-level files outside any project."""
    from memory_compiler.storage import safe_article_path
    import pytest
    with pytest.raises(ValueError):
        safe_article_path(".", "index.md")
    with pytest.raises(ValueError):
        safe_article_path("", "anything.md")


def test_safe_project_dir_rejects_traversal(knowledge_dir):
    """safe_project_dir must reject project names that escape KNOWLEDGE_DIR."""
    from memory_compiler.storage import safe_project_dir
    import pytest
    # Valid
    p = safe_project_dir("testproj")
    assert p == knowledge_dir / "testproj"
    # Traversal via project
    with pytest.raises(ValueError):
        safe_project_dir("../etc")
    with pytest.raises(ValueError):
        safe_project_dir("..")
    with pytest.raises(ValueError):
        safe_project_dir(".")
    with pytest.raises(ValueError):
        safe_project_dir("")
    with pytest.raises(ValueError):
        safe_project_dir("a/b")


def test_save_lesson_rejects_path_traversal_in_project(knowledge_dir):
    """save_lesson with project='../etc' must NOT create files outside KNOWLEDGE_DIR."""
    import asyncio
    from memory_compiler.handlers import save_lesson
    result = asyncio.run(save_lesson(
        topic="harmless title",
        content="some content",
        project="../etc",
    ))
    text = result[0].text
    assert "Небезопасный" in text or "unsafe" in text.lower() or "❌" in text
    # Verify no file created outside KNOWLEDGE_DIR
    import os
    parent = knowledge_dir.parent
    bad = parent / "etc"
    assert not bad.exists() or not any(bad.glob("*.md"))


def test_safe_path_rejects_traversal(knowledge_dir):
    """safe_project_path must reject path traversal attempts."""
    from memory_compiler.storage import safe_article_path
    proj_dir = knowledge_dir / "testproj"
    # Valid path inside project
    p = safe_article_path("testproj", "article.md")
    assert p == proj_dir / "article.md"
    # Path traversal via filename
    import pytest
    with pytest.raises(ValueError):
        safe_article_path("testproj", "../../../etc/passwd")
    with pytest.raises(ValueError):
        safe_article_path("testproj", "../infra/secret.md")
    # Path traversal via project
    with pytest.raises(ValueError):
        safe_article_path("../etc", "passwd")
    # Absolute path
    with pytest.raises(ValueError):
        safe_article_path("testproj", "/etc/passwd")


def test_extract_reflections_skips_negation():
    """Sentences with negation (не настроил / not fixed) must NOT be extracted as facts."""
    from memory_compiler.storage import extract_reflections
    content = (
        "Сегодня не настроил VPN — времени не хватило. "
        "Зато настроил nginx и подключил redis. "
        "Не удалил старые конфиги пока."
    )
    facts = extract_reflections(content)
    joined = " ".join(facts).lower()
    assert "nginx" in joined or "redis" in joined
    # negated sentences must not appear
    assert "не настроил vpn" not in joined
    assert "не удалил" not in joined


def test_append_reflections_atomic_write(knowledge_dir, tmp_path, monkeypatch):
    """append_reflections must write atomically (no half-written file)."""
    from memory_compiler.storage import append_reflections
    proj = knowledge_dir / "testproj"
    refl_path = proj / "_reflections.md"
    append_reflections("testproj", ["first fact"])
    # File exists and content valid
    assert refl_path.exists()
    text1 = refl_path.read_text(encoding="utf-8")
    assert "first fact" in text1
    # Re-append — must contain both, atomically
    append_reflections("testproj", ["second fact"])
    text2 = refl_path.read_text(encoding="utf-8")
    assert "first fact" in text2 and "second fact" in text2
    # No leftover .tmp file
    assert not (proj / "_reflections.md.tmp").exists()


def test_log_rotates_when_too_big(knowledge_dir, monkeypatch):
    """_log.md is rotated when it exceeds size threshold — older entries archived."""
    from memory_compiler import storage as _st
    # Force a small threshold for testing
    monkeypatch.setattr(_st, "LOG_ROTATE_BYTES", 200)
    for i in range(50):
        _st.log_event("testproj", "test_action", f"event number {i} with some details")
    log_path = knowledge_dir / "testproj" / "_log.md"
    archive = knowledge_dir / "testproj" / "_log.archive.md"
    # Current log smaller than 5x threshold (some rotation happened)
    assert log_path.stat().st_size < 200 * 5
    # Archive contains older entries
    assert archive.exists()


def test_extract_reflections_from_bullets():
    """Bullet-list content must yield atomic facts."""
    from memory_compiler.storage import extract_reflections
    content = (
        "Реализованы фичи:\n"
        "- Reranker подключён к search и get_context\n"
        "- log.md создаётся при каждом save_lesson\n"
        "- Lint находит сирот и битые ссылки\n"
        "\n"
        "Подробности в коде."
    )
    facts = extract_reflections(content)
    assert len(facts) >= 3
    joined = " ".join(facts)
    assert "Reranker" in joined
    assert "log.md" in joined
    assert "Lint" in joined


def test_extract_reflections_from_action_verbs():
    """Action sentences with настроил/добавил/исправил/обновил should be extracted."""
    from memory_compiler.storage import extract_reflections
    content = (
        "Сегодня настроил nginx reverse proxy на сервере app01. "
        "Также исправил баг с пустыми embeddings. "
        "Обновил версию до 1.7.0. "
        "Просто заметка для контекста."
    )
    facts = extract_reflections(content)
    joined = " ".join(facts).lower()
    assert "nginx" in joined or "reverse proxy" in joined
    assert "баг" in joined or "embeddings" in joined
    assert "1.7.0" in joined or "версию" in joined


def test_extract_reflections_skips_prose_without_facts():
    """Plain prose without clear facts must produce no reflections."""
    from memory_compiler.storage import extract_reflections
    content = (
        "Думаю над архитектурой. Возможно стоит посмотреть на это иначе. "
        "Нужно подумать о подходе."
    )
    facts = extract_reflections(content)
    # No bullets, no action verbs → empty
    assert facts == [] or len(facts) == 0


def test_finish_task_writes_reflections(knowledge_dir):
    """finish_task with structured content must populate _reflections.md."""
    import asyncio
    import memory_compiler.config as _cfg
    from memory_compiler.handlers import finish_task
    proj = knowledge_dir / "testproj"
    _cfg.PROJECTS = _cfg._discover_projects()
    asyncio.run(finish_task(
        topic="upgrade test",
        content=(
            "Сделано:\n"
            "- Подключил reranker\n"
            "- Добавил log.md\n"
            "- Расширил lint\n"
        ),
        project="testproj",
    ))
    refl_path = proj / "_reflections.md"
    assert refl_path.exists(), "_reflections.md should be created"
    text = refl_path.read_text(encoding="utf-8")
    assert "reranker" in text.lower()
    assert "log.md" in text.lower()
    assert "lint" in text.lower()


def test_reflections_fifo_caps_at_20(knowledge_dir):
    """_reflections.md must keep at most 20 entries (FIFO)."""
    from memory_compiler.storage import append_reflections
    facts = [f"Atomic fact number {i}" for i in range(25)]
    append_reflections("testproj", facts)
    refl_path = knowledge_dir / "testproj" / "_reflections.md"
    text = refl_path.read_text(encoding="utf-8")
    entries = [l for l in text.splitlines() if l.startswith("- [")]
    assert len(entries) == 20, f"Should cap at 20 entries, got {len(entries)}"
    # Newest preserved
    assert "Atomic fact number 24" in text
    # Oldest dropped
    assert "Atomic fact number 0" not in text


def test_log_event_creates_file(knowledge_dir):
    """log_event must create _log.md in project dir and append a line."""
    from memory_compiler.storage import log_event
    log_event("testproj", "ingest", "added 1 article from URL")
    log_path = knowledge_dir / "testproj" / "_log.md"
    assert log_path.exists(), "_log.md should be created in project dir"
    text = log_path.read_text(encoding="utf-8")
    assert "ingest" in text
    assert "added 1 article from URL" in text


def test_log_event_appends(knowledge_dir):
    """Successive log_event calls must append, not overwrite."""
    from memory_compiler.storage import log_event
    log_event("testproj", "ingest", "first entry")
    log_event("testproj", "lint", "second entry — 3 orphans")
    log_path = knowledge_dir / "testproj" / "_log.md"
    text = log_path.read_text(encoding="utf-8")
    assert "first entry" in text
    assert "second entry" in text
    # Both events recorded as separate lines
    lines = [l for l in text.splitlines() if l.startswith("- [")]
    assert len(lines) >= 2


def test_log_event_format_has_timestamp(knowledge_dir):
    """Each log line must begin with '- [YYYY-MM-DD HH:MM] **action** — details'."""
    import re
    from memory_compiler.storage import log_event
    log_event("testproj", "save_lesson", "topic: nginx ssl")
    text = (knowledge_dir / "testproj" / "_log.md").read_text(encoding="utf-8")
    pattern = re.compile(r"^- \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] \*\*save_lesson\*\* — topic: nginx ssl")
    assert any(pattern.match(line) for line in text.splitlines()), \
        f"No line matched expected format. Got:\n{text}"


def test_save_lesson_writes_log(knowledge_dir):
    """save_lesson handler must log a 'save_lesson' event to project _log.md."""
    import asyncio
    from memory_compiler.handlers import save_lesson
    asyncio.run(save_lesson(
        topic="docker proxy fix",
        content="Setting up reverse proxy in docker required setting headers.",
        project="testproj",
    ))
    log_path = knowledge_dir / "testproj" / "_log.md"
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "save_lesson" in text
    assert "docker proxy fix" in text


def test_lint_writes_log(knowledge_dir):
    """lint handler must log a 'lint' event with summary."""
    import asyncio
    from memory_compiler.handlers import lint as lint_handler
    asyncio.run(lint_handler(project="testproj"))
    log_path = knowledge_dir / "testproj" / "_log.md"
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "lint" in text


# ─── Relevance gate: auto_update_tracking must not touch unrelated entities ──
# Регрессия инцидента (гео-сущность): save_lesson про обновление (4-частная версия
# конфы) затёр поле address несвязанной гео-сущности. Корень — авто-апдейт
# трекера срабатывал на ЛЮБОЙ сущности с подходящим по типу ключом, без проверки,
# что заметка вообще ОТНОСИТСЯ к этой сущности. Это уже 6-й инцидент того же класса
# (v1.7.9/1.7.16/1.7.17/1.7.18) — лечим структурно: relevance-гейт.


def test_auto_update_tracking_does_not_overwrite_unreferenced_field(knowledge_dir):
    """Точная регрессия: 4-частная версия 1С (валидная как IP) не должна затирать
    поле address гео-сущности, которую заметка вообще не упоминает."""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking, load_tracking
    save_tracking_article("testproj", "coordinates",
                          {"lat": "48.4827", "lon": "135.0719", "address": "198.51.100.10"})

    note = ("Внедрение заселения через MAX в 1С:Отель. Версия конфы 9.2.5.75, "
            "требуется обновление до 9.2.6.57. Сканер ШК Mindeo MD6600HD.")
    updates = auto_update_tracking("testproj", note, topic="Внедрение фичи")

    data = load_tracking("testproj", "coordinates")
    assert data["current"]["address"] == "198.51.100.10", \
        f"address затёрт версией 1С: {data['current']}"
    assert updates == [], f"апдейтов быть не должно — заметка не про coordinates: {updates}"


def test_auto_update_tracking_skips_attacker_ip_in_incident_note(knowledge_dir):
    """Заметка-инцидент про чужой C2-IP не должна перезаписывать реальный IP
    нашего деплоя: заметка не ссылается на сущность deployment."""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking, load_tracking
    save_tracking_article("testproj", "deployment",
                          {"ip": "80.80.80.80", "host": "srv.example.com"})

    note = "Инцидент: нашли веб-шеллы, C2-сервер 80.81.82.83 (OVH Canada), SEO-спам."
    updates = auto_update_tracking("testproj", note, topic="incident report")

    cur = load_tracking("testproj", "deployment")["current"]
    assert cur["ip"] == "80.80.80.80", f"реальный IP затёрт IP атакующего: {cur['ip']!r}"
    assert updates == [], f"апдейтов быть не должно: {updates}"


def test_auto_update_tracking_updates_when_entity_referenced(knowledge_dir):
    """Гейт ПРОПУСКАЕТ апдейт, когда заметка явно ссылается на сущность по имени —
    основной сценарий пользы остаётся рабочим (новый IP той же публичной роли)."""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking, load_tracking
    save_tracking_article("testproj", "deployment",
                          {"ip": "80.80.80.80", "host": "srv.example.com"})

    note = "deployment переехал на новый IP 93.184.216.34."
    auto_update_tracking("testproj", note, topic="миграция")
    assert load_tracking("testproj", "deployment")["current"]["ip"] == "93.184.216.34"


def test_auto_update_tracking_ip_role_must_match(knowledge_dir):
    """Даже когда сущность упомянута, публичный IP не подменяется приватным (и наоборот):
    LAN- и WAN-адрес — разные сущности по природе. На авто-пути не угадываем."""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking, load_tracking
    save_tracking_article("testproj", "deployment", {"ip": "80.80.80.80"})  # публичный

    note = "deployment: видел в логе приватный 192.168.1.50."
    auto_update_tracking("testproj", note, topic="лог")
    assert load_tracking("testproj", "deployment")["current"]["ip"] == "80.80.80.80", \
        "публичный IP не должен подменяться приватным на авто-пути"


def test_address_not_classified_as_ip_key(knowledge_dir):
    """Ключ 'address' слишком неоднозначен (улица/гео/почта), не должен быть
    в IP-whitelist авто-апдейта."""
    from memory_compiler.storage import _AUTOUPDATE_KEY_WHITELIST
    assert "address" not in _AUTOUPDATE_KEY_WHITELIST["ip"], \
        "'address' не должен авто-классифицироваться как IP-поле"


# ─── find_existing_article: destructive auto-merge must be conservative ──────
# Регрессия: заметка про «заселение через MAX» молча дописалась в чужую статью
# «кнопка MAX-мессенджера» (общий редкий токен MAX). Порог 0.75 ниже e5-пола
# внутрипроектного сходства (~0.78), и нет запаса над вторым кандидатом.


def _fake_embed_env(monkeypatch, knowledge_dir, q_sim_a, q_sim_b):
    """Подготовить find_existing_article: 2 статьи a.md/b.md с заданным косинусом
    запроса к каждой. q=[1,0], вектор=[sim, sqrt(1-sim^2)] → dot == sim."""
    import numpy as np
    import memory_compiler.search as search_mod

    proj = knowledge_dir / "testproj"
    (proj / "a.md").write_text("# A\n\nрыба", encoding="utf-8")
    (proj / "b.md").write_text("# B\n\nрыба", encoding="utf-8")

    def unit(sim):
        return np.array([sim, (1.0 - sim * sim) ** 0.5])

    q = np.array([1.0, 0.0])
    emb = {"testproj/a.md": unit(q_sim_a), "testproj/b.md": unit(q_sim_b)}
    monkeypatch.setattr(search_mod, "_embeddings", emb)
    monkeypatch.setattr(search_mod, "encode_query", lambda text: q)

    class _FakeModel:
        def encode(self, texts, **kw):
            return np.array([q])
    monkeypatch.setattr(search_mod, "get_embed_model", lambda: _FakeModel())


def test_find_existing_article_no_merge_below_threshold(knowledge_dir, monkeypatch):
    """Слабое сходство (ниже e5-порога near-duplicate) → новая статья, не мёрж."""
    from memory_compiler.storage import find_existing_article
    _fake_embed_env(monkeypatch, knowledge_dir, q_sim_a=0.80, q_sim_b=0.78)
    result = find_existing_article("совсем другая тема xyz", "тело заметки", "testproj")
    assert result is None, f"не должно мёржить при sim 0.80: {result}"


def test_find_existing_article_no_merge_when_ambiguous(knowledge_dir, monkeypatch):
    """Два почти равных кандидата (нет запаса над вторым) → не угадываем, новая статья."""
    from memory_compiler.storage import find_existing_article
    _fake_embed_env(monkeypatch, knowledge_dir, q_sim_a=0.93, q_sim_b=0.91)
    result = find_existing_article("совсем другая тема xyz", "тело заметки", "testproj")
    assert result is None, f"при двух близких кандидатах мёрж неоднозначен: {result}"


def test_find_existing_article_merges_clear_winner(knowledge_dir, monkeypatch):
    """Явный единственный near-duplicate (высокий sim, большой отрыв) → мёрж."""
    from memory_compiler.storage import find_existing_article
    _fake_embed_env(monkeypatch, knowledge_dir, q_sim_a=0.95, q_sim_b=0.50)
    result = find_existing_article("совсем другая тема xyz", "тело заметки", "testproj")
    assert result is not None and result.name == "a.md", f"должен смёржить в a.md: {result}"


# ─── Контекст-гард версии (v1.7.20): 4-октетная версия после cue-слова ≠ IP ──
# Узкий остаток v1.7.19: dotted-quad версии 1С (9.2.5.75) — валидный IP по форме.
# Если число стоит сразу после version-слова, это версия, а не сетевой адрес.
# Консервативно: только СМЕЖНЫЙ cue, чтобы не выкинуть настоящий IP из фразы с версией.


def test_extract_facts_version_after_cue_not_ip():
    """'Версия конфы 9.2.5.75' — число после cue-слова не извлекается как IP."""
    from memory_compiler.storage import extract_facts_from_text
    facts = extract_facts_from_text("Версия конфы 9.2.5.75 на проде")
    assert "9.2.5.75" not in facts.get("ip", []), f"версия 1С принята за IP: {facts}"


def test_extract_facts_real_ip_after_noncue_word_kept():
    """Контроль: настоящий IP после обычного слова по-прежнему извлекается."""
    from memory_compiler.storage import extract_facts_from_text
    facts = extract_facts_from_text("Сервер развёрнут на 80.80.80.80")
    assert "80.80.80.80" in facts.get("ip", []), f"настоящий IP потерян: {facts}"


def test_auto_update_tracking_version_after_cue_not_written_to_ip(knowledge_dir):
    """E2E остаток: даже когда сущность упомянута, версия после cue-слова не должна
    попасть в ip/host (она не IP)."""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking, load_tracking
    save_tracking_article("testproj", "deployment", {"ip": "80.80.80.80"})
    note = "deployment: обновили конфу до версия 9.2.5.75."
    auto_update_tracking("testproj", note, topic="обновление")
    cur = load_tracking("testproj", "deployment")["current"]
    assert cur["ip"] == "80.80.80.80", f"версия 1С попала в ip: {cur['ip']!r}"


# ─── v1.7.21: выравнивание IP-валидации + логи авто-мутаций + connector-cue ──


def test_extract_facts_rejects_invalid_octet():
    """Выравнивание с _extract_facts: октет >255 — не IP (битая версия/сборка)."""
    from memory_compiler.storage import extract_facts_from_text
    facts = extract_facts_from_text("роутер 1.2.3.300 в сети")
    assert "1.2.3.300" not in facts.get("ip", []), f"невалидный октет принят за IP: {facts}"


def test_extract_facts_excludes_zero_net():
    """0.0.0.0/8 — не хост (это 4-частная версия 0.x)."""
    from memory_compiler.storage import extract_facts_from_text
    facts = extract_facts_from_text("артефакт 0.2.0.5 собран")
    assert "0.2.0.5" not in facts.get("ip", []), f"0.x принят за IP: {facts}"


def test_extract_facts_excludes_cidr():
    """CIDR-подсеть — не host-IP (нельзя сравнивать с адресом хоста)."""
    from memory_compiler.storage import extract_facts_from_text
    facts = extract_facts_from_text("сервер в подсети 10.10.0.0/16 за фаерволом")
    assert "10.10.0.0" not in facts.get("ip", []), f"CIDR-сеть принята за host-IP: {facts}"


def test_extract_facts_version_after_cue_with_connector():
    """Connector-cue: 'обновление до 9.2.6.57' — версия через связку 'до', не IP."""
    from memory_compiler.storage import extract_facts_from_text
    facts = extract_facts_from_text("Требуется обновление до 9.2.6.57 на стойке")
    assert "9.2.6.57" not in facts.get("ip", []), f"версия через connector принята за IP: {facts}"


def test_auto_update_tracking_logs_changes(knowledge_dir):
    """Наблюдаемость: авто-апдейт трекера пишет событие в <project>/_log.md."""
    from memory_compiler.storage import save_tracking_article, auto_update_tracking
    save_tracking_article("testproj", "deployment", {"version": "1.0.0"})
    auto_update_tracking("testproj", "deployment обновлён до 1.1.0", "deploy")
    log_path = knowledge_dir / "testproj" / "_log.md"
    assert log_path.exists(), "авто-апдейт трекера должен логироваться в _log.md"
    text = log_path.read_text(encoding="utf-8")
    assert "auto_update" in text and "deployment" in text, f"нет записи об авто-апдейте: {text!r}"
