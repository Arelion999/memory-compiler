"""structuredContent + outputSchema для search.

С v1.87.0 у search одна форма выдачи: structuredContent и единственный текстовый
блок несут один и тот же JSON. Футер свежести и ошибка параметра уходят в notice:
отдельный текстовый блок Claude Code модели не показывает.
"""
import asyncio
import json

from mcp.types import TextContent

from memory_compiler import handlers
from memory_compiler.tools import list_tools, call_tool, _search_response


def test_search_tool_declares_output_schema():
    tools = {t.name: t for t in asyncio.run(list_tools())}
    osch = tools["search"].outputSchema
    assert osch is not None
    assert "results" in osch["properties"]
    assert set(osch["required"]) == {"query", "count", "results"}


def test_text_block_equals_structured_content(knowledge_dir):
    content, structured = asyncio.run(call_tool("search", {"query": "docker", "project": "testproj"}))
    assert len(content) == 1 and content[0].type == "text"
    assert json.loads(content[0].text) == structured
    assert structured["count"] == len(structured["results"]) >= 1


def test_secret_article_is_present_in_structured_output(knowledge_dir):
    """РЕГРЕСС v1.53.0: секретное попадание обязано быть в выдаче с флагом —
    сценарий владельца «искал креды, статью с ними не вижу»."""
    proj = knowledge_dir / "testproj"
    (proj / "secret_docker_creds.md").write_text(
        "# Доступы к docker-реестру\n\n"
        "**Дата:** 2026-01-01 10:00\n"
        "**Проект:** testproj\n"
        "**Теги:** docker, пароль\n\n"
        "## Записи\n\nENC:gAAAAABsecretpayload\n",
        encoding="utf-8",
    )
    content, structured = asyncio.run(call_tool("search", {"query": "docker", "project": "testproj"}))
    hit = next(r for r in structured["results"] if "secret_docker_creds" in r["file"])
    assert hit["secret"] is True, "панель рисует замок по этому флагу"
    assert hit["project"] == "testproj" and hit["file"].endswith(".md")
    assert "ENC:" not in content[0].text, "содержимое секрета в выдачу не попадает"


def test_footer_goes_into_notice_not_a_separate_block():
    payload = {"query": "q", "count": 0, "results": []}
    token = handlers.search_payload_var.set(payload)
    try:
        blocks = [TextContent(type="text", text=handlers.search_json(payload)),
                  TextContent(type="text", text="\n\n📌 подсказка про проект")]
        content, structured = _search_response("q", blocks)
    finally:
        handlers.search_payload_var.reset(token)
    assert len(content) == 1
    assert structured["notice"] == "📌 подсказка про проект"
    assert json.loads(content[0].text) == structured


def test_missing_payload_gives_empty_result_and_keeps_the_error():
    token = handlers.search_payload_var.set(None)
    try:
        content, structured = _search_response(
            "q", [TextContent(type="text", text="❌ Небезопасный параметр: x")])
    finally:
        handlers.search_payload_var.reset(token)
    assert (structured["query"], structured["count"], structured["results"]) == ("q", 0, [])
    assert structured["notice"].startswith("❌"), "ошибку модель обязана увидеть"
    assert json.loads(content[0].text) == structured


def test_call_tool_non_search_stays_list(knowledge_dir):
    out = asyncio.run(call_tool("list_projects", {}))
    assert isinstance(out, list)  # без outputSchema — обычный список content-блоков
