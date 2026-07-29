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


def test_secret_article_is_present_in_structured_output(knowledge_dir):
    """РЕГРЕСС. structuredContent собирался из resource_link-блоков, а ссылок на
    секреты нет НАМЕРЕННО (как ресурс секрет недоступен). Из-за этого панель MCP
    Apps не показывала секретные попадания ВООБЩЕ — ни строкой, ни замком, — и её
    счётчик «найдено» расходился с текстовой выдачей того же вызова. Молча:
    ни исключения, ни предупреждения. Сценарий владельца — «искал креды, статью
    с ними не вижу».
    """
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

    names = [r["name"] for r in structured["results"]]
    assert any("secret_docker_creds" in n for n in names), (
        f"секретная статья пропала из structuredContent: {names}"
    )
    hit = next(r for r in structured["results"] if "secret_docker_creds" in r["name"])
    assert hit["secret"] is True, "секрет обязан быть помечен — панель рисует по этому флагу замок"
    assert hit["project"] == "testproj" and hit["file"].endswith(".md"), (
        "панель открывает статью через read_article(project, filename) — оба поля обязаны быть"
    )
    assert structured["count"] == len(structured["results"])
    # Ссылок на секрет по-прежнему нет: как ресурс он недоступен, и это не меняется.
    links = [b for b in content if getattr(b, "type", None) == "resource_link"]
    assert not [b for b in links if "secret_" in (b.name or "")]


def test_structured_count_matches_the_text_output(knowledge_dir):
    """Два представления одного вызова не должны расходиться."""
    proj = knowledge_dir / "testproj"
    (proj / "secret_docker_creds.md").write_text(
        "# Доступы к docker-реестру\n\n**Теги:** docker\n\n## Записи\n\nENC:x\n", encoding="utf-8")
    content, structured = asyncio.run(call_tool("search", {"query": "docker", "project": "testproj"}))
    headings = content[0].text.count("\n### ")
    assert structured["count"] == headings, (
        f"панель покажет {structured['count']}, текст — {headings}"
    )


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
