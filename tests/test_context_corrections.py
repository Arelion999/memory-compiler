"""Контекст обязан показать поправку до отменённого вывода, без секретного тела."""
import os

import pytest

from memory_compiler import handlers, handlers_search, storage


@pytest.fixture
def corpus(knowledge_dir, monkeypatch):
    monkeypatch.setattr(handlers_search, "RERANK_ENABLED", False)
    return knowledge_dir


def article(corpus, name, title, body, project="testproj"):
    path = corpus / project / name
    path.write_text(f"# {title}\n\n**Теги:** тест\n\n{body}\n", encoding="utf-8")
    return {"project": project, "file": name, "title": title, "score": 95,
            "preview": f"# {title}\n{body}"}


def retrieval(monkeypatch, hits):
    async def find(query, project="all", limit=20):
        return [dict(h) for h in hits if project == "all" or h["project"] == project][:limit]
    monkeypatch.setattr(handlers, "_whoosh_async", find)


async def context(mode):
    if mode == "query":
        return (await handlers.get_context("testproj", "контур платформы"))[0].text
    if mode == "recent":
        return (await handlers.get_context("testproj"))[0].text
    return (await handlers.start_task("контур платформы", "testproj"))[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["query", "recent", "found", "deps"])
async def test_correction_precedes_old_context(corpus, monkeypatch, mode):
    project = "general" if mode == "deps" else "testproj"
    old = article(corpus, "old.md", "Прежний вывод", "Старое тело", project)
    article(corpus, "fix.md", "Исправленный вывод", "Контур НЕ нужен", project)
    storage.mark_superseded(project, "old.md", "fix.md", "Исправленный вывод")
    # Старая статья самая свежая, поправка может не попасть в обычный top-N.
    os.utime(corpus / project / "old.md", (2000000000, 2000000000))
    retrieval(monkeypatch, [old])
    if mode == "deps":
        storage.write_project_deps("testproj", ["general"])
    text = await context(mode)
    assert "Контур НЕ нужен" in text
    assert text.index("Исправленный вывод") < text.index("Прежний вывод")
    old_block = text[text.index("Прежний вывод"):]
    assert "ОТМЕНЕНА" in old_block and "fix.md" in old_block


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["query", "recent", "found", "deps"])
@pytest.mark.parametrize("chain", [False, True])
async def test_unavailable_or_superseded_correction_is_not_presented_as_current(
        corpus, monkeypatch, mode, chain):
    project = "general" if mode == "deps" else "testproj"
    old = article(corpus, "old.md", "Прежний вывод", "Старое тело", project)
    storage.mark_superseded(project, "old.md", "fix.md", "Промежуточная поправка")
    if chain:
        article(corpus, "fix.md", "Промежуточная поправка", "Второй вывод", project)
        storage.mark_superseded(project, "fix.md", "latest.md", "Последнее уточнение")
    retrieval(monkeypatch, [old])
    if mode == "deps":
        storage.write_project_deps("testproj", ["general"])
    text = await context(mode)
    assert "ОТМЕНЕНА" in text and "fix.md" in text
    if chain:
        assert "latest.md" in text


@pytest.mark.asyncio
async def test_same_filename_in_other_project_is_not_dropped(corpus, monkeypatch):
    first = article(corpus, "same.md", "Первый проект", "Первое тело")
    second = article(corpus, "same.md", "Другой проект", "Второе тело", "general")
    retrieval(monkeypatch, [first, second])
    text = await context("query")
    assert "Первое тело" in text and "Второе тело" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["query", "recent", "found", "deps"])
@pytest.mark.parametrize("secret_name", ["secret_fix.md", "flagged.md"])
async def test_correction_never_exposes_secret_content(corpus, monkeypatch, mode, secret_name):
    project = "general" if mode == "deps" else "testproj"
    old = article(corpus, "old.md", "Прежний вывод", "Старое тело", project)
    path = corpus / project / secret_name
    path.write_text("# Закрытая поправка\n\n**Секрет:** да\n\nPRIVATE_BODY_SENTINEL\n", encoding="utf-8")
    storage.mark_superseded(project, "old.md", secret_name, "Закрытая поправка")
    retrieval(monkeypatch, [old])
    if mode == "deps":
        storage.write_project_deps("testproj", ["general"])
    text = await context(mode)
    assert "PRIVATE_BODY_SENTINEL" not in text
    assert "ОТМЕНЕНА" in text and secret_name in text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["found", "deps"])
async def test_changed_correction_keeps_new_warning_in_same_chat(corpus, monkeypatch, mode):
    from memory_compiler import freshness
    freshness.reset()
    project = "general" if mode == "deps" else "testproj"
    title = ("Промежуточная поправка " + "подробный контекст " * 8).strip()
    old = article(corpus, "old.md", "Прежний вывод", "OLD_BODY", project)
    article(corpus, "fix.md", title, "INTERMEDIATE_BODY", project)
    article(corpus, "latest.md", "Действующая поправка", "CURRENT_BODY", project)
    storage.mark_superseded(project, "old.md", "fix.md", title)
    retrieval(monkeypatch, [old])
    if mode == "deps":
        storage.write_project_deps("testproj", ["general"])
    key = freshness.chat_key_var.set("c:review")
    try:
        first = await context(mode)
        assert "INTERMEDIATE_BODY" in first
        storage.mark_superseded(project, "fix.md", "latest.md", "Действующая поправка")
        second = await context(mode)
        assert "latest.md" in second
        fix_block = second[second.index("Промежуточная поправка"):second.index("Прежний вывод")]
        assert "ОТМЕНЕНА" in fix_block
        assert "INTERMEDIATE_BODY" not in second
        third = await context(mode)
        assert "latest.md" in third and "ОТМЕНЕНА" in third
    finally:
        freshness.chat_key_var.reset(key)
        freshness.reset()


@pytest.mark.parametrize("block_key", ["found", "deps"])
@pytest.mark.parametrize("long_warning", [False, True])
def test_budget_never_shows_cancelled_title_without_complete_warning(block_key, long_warning):
    title = "### [testproj] Cancelled rule " + "long title " * 50
    warning = "⚠️ ОТМЕНЕНА — поправка: testproj/fix.md"
    if long_warning:
        warning += " — " + "replacement title " * 50
    item = title + "\n" + warning + "\nOLD_BODY"
    block = handlers._Block(block_key, "## Context", [item], weight=1)
    report = []
    text = handlers._render_block(block, budget=300, report=report)
    if long_warning:
        assert "Cancelled rule" not in text
    else:
        assert "Cancelled rule" in text
        assert warning in text
    assert not report, "a trimmed or hidden item must not count as fully shown"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["found", "deps"])
async def test_budget_keeps_cancellation_in_start_task(corpus, monkeypatch, mode):
    from memory_compiler import handlers_sessions
    monkeypatch.setattr(handlers_sessions, "START_BUDGET", 350)
    project = "general" if mode == "deps" else "testproj"
    old = article(corpus, "old.md", "Cancelled rule " + "long title " * 60, "OLD_BODY", project)
    storage.mark_superseded(project, "old.md", "fix.md", "Replacement")
    retrieval(monkeypatch, [old])
    if mode == "deps":
        storage.write_project_deps("testproj", ["general"])
    text = await context(mode)
    assert "Cancelled rule" in text
    assert "ОТМЕНЕНА" in text and f"{project}/fix.md" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["query", "recent", "found", "deps"])
async def test_direct_secret_preview_is_redacted_using_file_metadata(corpus, monkeypatch, mode):
    project = "general" if mode == "deps" else "testproj"
    hit = article(corpus, "flagged.md", "Secret title", "PRIVATE_BODY_SENTINEL", project)
    path = corpus / project / "flagged.md"
    path.write_text("# Secret title\n\n**Секрет:** да\n\nPRIVATE_BODY_SENTINEL\n", encoding="utf-8")
    # У поискового превью нет метафлага; источник истины — файл.
    retrieval(monkeypatch, [hit])
    if mode == "deps":
        storage.write_project_deps("testproj", ["general"])
    text = await context(mode)
    assert "PRIVATE_BODY_SENTINEL" not in text
    assert "🔒" in text and "read_article" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["query", "recent", "found", "deps"])
async def test_correction_cannot_read_outside_its_project(corpus, monkeypatch, mode):
    project = "general" if mode == "deps" else "testproj"
    outside = corpus.parent / "outside.md"
    outside.write_text("# Outside\n\nOUTSIDE_BODY_SENTINEL\n", encoding="utf-8")
    old = article(corpus, "old.md", "Old article", "Old body", project)
    assert storage.mark_superseded(project, "old.md", "../../outside.md", "Invalid correction")
    retrieval(monkeypatch, [old])
    if mode == "deps":
        storage.write_project_deps("testproj", ["general"])
    text = await context(mode)
    assert "OUTSIDE_BODY_SENTINEL" not in text
    assert "../../outside.md" not in text
    assert "поправка отсутствует" in text


@pytest.mark.asyncio
async def test_context_masks_secret_with_markdown_in_yaml(corpus, monkeypatch):
    hit = article(corpus, "flagged.md", "Secret title", "PRIVATE_BODY_SENTINEL")
    (corpus / "testproj" / "flagged.md").write_text(
        "---\ncontexts:\n  example: |\n    ## Internal heading\n---\n"
        "# Secret title\n\n**Секрет:** да\n\nPRIVATE_BODY_SENTINEL\n", encoding="utf-8"
    )
    retrieval(monkeypatch, [hit])
    assert "PRIVATE_BODY_SENTINEL" not in await context("query")


@pytest.mark.asyncio
async def test_correction_does_not_change_start_task_project(corpus, monkeypatch):
    primary = article(corpus, "first.md", "Main result", "Main body")
    old = article(corpus, "old.md", "Old article", "Old body", "general")
    old["score"] = 60
    article(corpus, "fix.md", "Correction", "Corrected body", "general")
    storage.mark_superseded("general", "old.md", "fix.md", "Correction")
    (corpus / "testproj" / "_active_context.md").write_text(
        "- [2026-01-01] Контур primary project activity\n", encoding="utf-8"
    )
    retrieval(monkeypatch, [primary, old])
    text = (await handlers.start_task("контур платформы", "all"))[0].text
    assert "primary project activity" in text
    assert "Corrected body" in text


@pytest.mark.asyncio
async def test_cancelled_decision_and_runbook_are_not_listed_as_current(corpus, monkeypatch):
    hits = [article(corpus, f"note{i}.md", f"Note {i}", "Relevant result") for i in range(3)]
    decision = article(corpus, "decision_old.md", "Old decision", "## Решение\nSTALE_DECISION")
    runbook = corpus / "testproj" / "old_runbook.md"
    runbook.write_text("# Контур платформы obsolete runbook\n\n**Тип:** runbook\n\n- [ ] Old step\n",
                       encoding="utf-8")
    for filename in (decision["file"], runbook.name):
        storage.mark_superseded("testproj", filename, "fix.md", "Correction")
    retrieval(monkeypatch, [*hits, decision])
    text = await context("found")
    assert "STALE_DECISION" not in text
    assert "obsolete runbook" not in text
