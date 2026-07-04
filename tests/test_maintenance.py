"""Tests for maintenance module (ремедиация issue #2)."""


def test_dedupe_all_articles_walker(knowledge_dir, monkeypatch):
    """Обходчик: чинит дубли, dry-run не пишет, секреты и _служебные не трогает."""
    import memory_compiler.maintenance as m
    monkeypatch.setattr(m, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(m, "PROJECTS", ["testproj"])

    ts = "2026-07-04 12:00"
    proj = knowledge_dir / "testproj"
    dup = proj / "dup.md"
    dup.write_text(
        f"# Д\n\n**Дата:** {ts}\n**Обновлено:** {ts}\n\n"
        f"## Записи\n\n### {ts}\nтекст\n\n### {ts}\nтекст\n",
        encoding="utf-8",
    )
    secret = proj / "secret_creds.md"
    secret_text = (f"# Креды\n\n**Теги:** secret\n\n## Записи\n\n"
                   f"### {ts}\nENC:abc\n\n### {ts}\nENC:abc\n")
    secret.write_text(secret_text, encoding="utf-8")
    service = proj / "_active_context.md"
    service.write_text("# Активный контекст\n\n- [x] a\n- [x] a\n", encoding="utf-8")

    # dry-run: находит, но не пишет
    touched, removed = m.dedupe_all_articles(dry_run=True)
    assert (touched, removed) == (1, 1)
    assert dup.read_text(encoding="utf-8").count(f"### {ts}") == 2

    # боевой прогон: чинит dup.md, не трогает секрет и служебный файл
    touched, removed = m.dedupe_all_articles()
    assert (touched, removed) == (1, 1)
    assert dup.read_text(encoding="utf-8").count(f"### {ts}") == 1
    assert secret.read_text(encoding="utf-8") == secret_text
    assert "**Обновлено:**" not in dup.read_text(encoding="utf-8")
