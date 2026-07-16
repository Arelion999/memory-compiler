"""Resource links во всех поисковых инструментах (расширение v1.15.0 → все search-tools).

search/search_by_tag/search_decisions/search_error/search_snippets возвращают
ResourceLink на memory://<проект>/<файл>. Проверяем by_tag (индекс не нужен) и хелпер.
"""
import asyncio

from memory_compiler.handlers import search_by_tag, _resource_links


def test_search_by_tag_has_resource_links(knowledge_dir):
    # test_article.md в фикстуре имеет теги "docker, test"
    result = asyncio.run(search_by_tag("docker", "testproj"))
    assert result[0].type == "text"
    links = [b for b in result if getattr(b, "type", None) == "resource_link"]
    assert any(str(l.uri) == "memory://testproj/test_article.md" for l in links)


def test_resource_links_skips_secrets_and_dedupes():
    items = [
        {"project": "p", "file": "a.md", "title": "A"},
        {"project": "p", "file": "a.md", "title": "A dup"},   # дубль
        {"project": "p", "file": "secret_pw.md", "title": "S"},  # секрет
        {"project": "p", "file": "b.md"},
    ]
    links = _resource_links(items)
    uris = [str(l.uri) for l in links]
    assert uris == ["memory://p/a.md", "memory://p/b.md"], "секрет исключён, дубль схлопнут"


def test_resource_links_ignores_incomplete_items():
    links = _resource_links([{"project": "p"}, {"file": "x.md"}, {}])
    assert links == []
