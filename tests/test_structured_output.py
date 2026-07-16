"""structuredContent + outputSchema для search (P2).

search объявляет outputSchema и возвращает машиночитаемый structuredContent
(query/count/results[uri,name,title,score]) рядом с человекочитаемым текстом.
Строится из resource_link-блоков — программный клиент получает список статей.
"""
import asyncio

from mcp.types import TextContent, ResourceLink

from memory_compiler.tools import list_tools, call_tool, _build_search_structured


def test_search_tool_declares_output_schema():
    tools = {t.name: t for t in asyncio.run(list_tools())}
    osch = tools["search"].outputSchema
    assert osch is not None
    assert "results" in osch["properties"]
    assert set(osch["required"]) == {"query", "count", "results"}


def test_build_search_structured_from_blocks():
    blocks = [
        TextContent(type="text", text="summary"),
        ResourceLink(type="resource_link", uri="memory://p/a.md", name="p/a.md",
                     title="A", description="score: 90"),
        ResourceLink(type="resource_link", uri="memory://p/b.md", name="p/b.md",
                     title="B", description="score: 80"),
    ]
    s = _build_search_structured("nginx", blocks)
    assert s["query"] == "nginx"
    assert s["count"] == 2
    assert s["results"][0]["name"] == "p/a.md"
    assert s["results"][0]["score"] == "score: 90"
    assert str(s["results"][0]["uri"]).startswith("memory://p/a.md")


def test_call_tool_search_returns_structured(knowledge_dir):
    out = asyncio.run(call_tool("search", {"query": "docker", "project": "testproj"}))
    # CombinationContent: (content_blocks, structuredContent)
    assert isinstance(out, tuple)
    content, structured = out
    assert content[0].type == "text"
    assert structured["query"] == "docker"
    assert structured["count"] >= 1
    assert structured["results"][0]["uri"].startswith("memory://testproj/")


def test_call_tool_non_search_stays_list(knowledge_dir):
    out = asyncio.run(call_tool("list_projects", {}))
    assert isinstance(out, list)  # без outputSchema — обычный список content-блоков
