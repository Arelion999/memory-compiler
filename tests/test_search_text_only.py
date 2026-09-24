"""Поисковые инструменты отдают один текстовый блок (v1.91.0).

resource_link дублировал каждую строку выдачи: модель получала и текст, и ссылку
с percent-кодированной кириллицей. У search ссылки сняты в v1.87.0, у остальных —
здесь.
"""
import asyncio

import pytest

from memory_compiler import handlers

DECISION = ("# Выбор брокера\n\n**Дата:** 2026-03-01 10:00\n**Тип:** decision\n**Теги:** mqtt\n\n"
            "## Решение\nMosquitto\n")
SNIPPET = "# Пример конфига\n\n**Теги:** nginx\n\n```nginx\nserver { listen 80; }\n```\n"


@pytest.fixture
def fake_search(knowledge_dir, monkeypatch):
    (knowledge_dir / "testproj" / "decision_broker.md").write_text(DECISION, encoding="utf-8")
    (knowledge_dir / "testproj" / "snip.md").write_text(SNIPPET, encoding="utf-8")
    hits = [
        {"project": "testproj", "file": "decision_broker.md", "title": "Выбор брокера",
         "score": 80, "preview": "# Выбор брокера\nMosquitto"},
        {"project": "testproj", "file": "snip.md", "title": "Пример конфига",
         "score": 70, "preview": "# Пример конфига\nserver"},
    ]

    async def fake(query, project="all", limit=10):
        return [dict(h) for h in hits]

    monkeypatch.setattr(handlers, "_whoosh_async", fake)


def test_search_decisions_is_text_only(fake_search):
    out = asyncio.run(handlers.search_decisions("брокер", "testproj"))
    assert [c.type for c in out] == ["text"] and "Выбор брокера" in out[0].text


def test_search_error_is_text_only(fake_search):
    out = asyncio.run(handlers.search_error("ConnectionRefusedError: [Errno 111]", "testproj"))
    assert [c.type for c in out] == ["text"] and "Выбор брокера" in out[0].text


def test_search_snippets_is_text_only(fake_search):
    out = asyncio.run(handlers.search_snippets("listen", project="testproj"))
    assert [c.type for c in out] == ["text"] and "listen 80" in out[0].text
