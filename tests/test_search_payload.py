"""Компактная выдача search (v1.87.0): один JSON без превью и resource_link.

Claude Code при объявленном outputSchema показывает модели structuredContent и
прячет текстовые блоки: превью v1.67.0 не дошло до модели ни в одном из 723
поисков недели (замер 15.09.2026). Поэтому выдача — один JSON, и в нём обязано
быть всё, что модель видела раньше, в том же порядке.
"""
import json

import pytest

from memory_compiler import handlers


def _r(project, file, title, score=90.0, **extra):
    return {"project": project, "file": file, "title": title, "score": score,
            "preview": f"# {title}\nтекст", **extra}


def test_payload_keeps_order_count_and_fields_of_ranking():
    results = [_r("p", "a.md", "A", 99.0), _r("p", "b.md", "B", 80.0), _r("q", "c.md", "C", 70.0)]
    payload = handlers._search_payload("nginx", results, {})
    assert payload["query"] == "nginx"
    assert payload["count"] == 3
    assert [(i["project"], i["file"]) for i in payload["results"]] == [
        ("p", "a.md"), ("p", "b.md"), ("q", "c.md")]
    first = payload["results"][0]
    assert first["title"] == "A"
    assert first["score"] == "score: 99.0"
    assert first["secret"] is False


def test_payload_has_no_preview_and_no_optional_noise():
    payload = handlers._search_payload("q", [_r("p", "a.md", "A")], {})
    item = payload["results"][0]
    assert "preview" not in item
    for key in ("superseded_by", "correction"):
        assert key not in item, f"{key} появляется только когда есть что сказать"
    assert "fallback_from" not in payload


def test_release1_still_sends_deprecated_uri_and_name():
    item = handlers._search_payload("q", [_r("p", "a.md", "A")], {})["results"][0]
    assert item["uri"] == "memory://p/a.md"
    assert item["name"] == "p/a.md"


def test_secret_is_flagged_by_the_map():
    payload = handlers._search_payload("q", [_r("p", "secret_x.md", "S")], {"p/secret_x.md": True})
    assert payload["results"][0]["secret"] is True


def test_superseded_and_correction_marks():
    results = [_r("p", "new.md", "Поправка", is_correction=True),
               _r("p", "old.md", "Старое", superseded_by=("new.md", "Поправка"))]
    items = handlers._search_payload("q", results, {})["results"]
    assert items[0]["correction"] is True
    assert items[1]["superseded_by"] == "new.md"


def test_fallback_from_is_reported():
    payload = handlers._search_payload("q", [_r("z", "a.md", "A")], {}, fallback_from="p")
    assert payload["fallback_from"] == "p"


def test_search_json_keeps_cyrillic_literal_and_compact():
    text = handlers.search_json({"query": "пароль", "count": 0, "results": []})
    assert text == '{"query":"пароль","count":0,"results":[]}'


@pytest.mark.asyncio
async def test_handler_returns_single_json_block_equal_to_payload(knowledge_dir):
    result = await handlers.search("docker", "testproj")
    assert len(result) == 1 and result[0].type == "text"
    payload = json.loads(result[0].text)
    assert payload == handlers.search_payload_var.get()
    assert any(i["file"] == "test_article.md" and i["title"] == "Test Article"
               for i in payload["results"])


@pytest.mark.asyncio
async def test_empty_search_is_the_same_json_shape(knowledge_dir):
    result = await handlers.search("zzzнетничего", "testproj")
    assert json.loads(result[0].text) == {"query": "zzzнетничего", "count": 0, "results": []}


import asyncio

import jsonschema

from memory_compiler.tools import list_tools


def _schema():
    return {t.name: t for t in asyncio.run(list_tools())}["search"].outputSchema


# Схема 1.86.1 — её держат в кэше tools/list клиенты, не перезапускавшие приложение.
OLD_SCHEMA_1_86_1 = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "count": {"type": "integer"},
        "results": {"type": "array", "items": {
            "type": "object",
            "properties": {"uri": {"type": "string"}, "name": {"type": "string"},
                           "title": {"type": "string"}, "score": {"type": "string"},
                           "project": {"type": "string"}, "file": {"type": "string"},
                           "secret": {"type": "boolean"}},
            "required": ["uri", "name"]}},
        "notice": {"type": "string"},
    },
    "required": ["query", "count", "results"],
}


def _full_payload():
    results = [_r("p", "new.md", "Поправка", is_correction=True),
               _r("p", "old.md", "Старое", superseded_by=("new.md", "Поправка")),
               _r("p", "secret_x.md", "S")]
    payload = handlers._search_payload("q", results, {"p/secret_x.md": True}, fallback_from="z")
    payload["notice"] = "📌 подсказка"
    return payload


def test_current_schema_accepts_payload():
    jsonschema.validate(_full_payload(), _schema())


def test_schema_cached_by_old_clients_accepts_release1_payload():
    """Claude Code 2.1.270 проверяет structuredContent по схеме из кэша tools/list,
    Desktop держит кэш до полного перезапуска: payload релиза 1 обязан пройти старую схему."""
    jsonschema.validate(_full_payload(), OLD_SCHEMA_1_86_1)


def test_result_requires_what_read_article_needs():
    schema = _schema()
    item = schema["properties"]["results"]["items"]
    assert set(item["required"]) == {"title", "project", "file"}
    assert {"superseded_by", "correction"} <= set(item["properties"])
    assert "fallback_from" in schema["properties"]
