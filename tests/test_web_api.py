"""Тесты web-endpoints, отвечающих за фасетную фильтрацию (теги + проект).

Регрессии, которые они стерегут (v1.7.22):
  - клик по тегу-чипу запускал полнотекстовый поиск вместо точной
    фильтрации по тегу → счётчик "1c (50)" не совпадал с выдачей (0 статей);
  - выпадающий список проектов не сужал ни статьи, ни облако тегов.
"""
import asyncio
import json

from memory_compiler.api import web_tags, web_by_tag, web_search


class FakeRequest:
    """Минимальный stand-in под starlette.Request для прямого вызова endpoint'ов."""

    def __init__(self, query=None, path=None):
        self.query_params = query or {}
        self.path_params = path or {}


def _json(resp):
    return json.loads(resp.body)


def _write(kd, project, name, tags, title="T"):
    p = kd / project
    p.mkdir(exist_ok=True)
    (p / name).write_text(
        f"# {title}\n\n"
        f"**Дата:** 2026-01-01 10:00\n"
        f"**Проект:** {project}\n"
        f"**Теги:** {tags}\n\n"
        "## Записи\nтело статьи\n",
        encoding="utf-8",
    )


def test_by_tag_returns_every_article_with_that_tag(knowledge_dir):
    _write(knowledge_dir, "testproj", "a1.md", "1c, deploy")
    _write(knowledge_dir, "testproj", "a2.md", "1c")
    _write(knowledge_dir, "general", "g1.md", "1c")
    data = _json(asyncio.run(web_by_tag(FakeRequest(query={"tag": "1c"}))))
    assert {a["file"] for a in data["articles"]} == {"a1.md", "a2.md", "g1.md"}
    # форма элементов совместима с renderResults в UI
    assert all({"title", "project", "file", "preview"} <= a.keys() for a in data["articles"])


def test_by_tag_count_matches_tags_facet(knowledge_dir):
    """Гарантия консистентности: сколько чип насчитал — столько статей и вернётся."""
    _write(knowledge_dir, "testproj", "a1.md", "1c")
    _write(knowledge_dir, "testproj", "a2.md", "1c")
    _write(knowledge_dir, "general", "g1.md", "1c")
    facet = _json(asyncio.run(web_tags(FakeRequest())))
    count_1c = next(t["count"] for t in facet["tags"] if t["tag"] == "1c")
    arts = _json(asyncio.run(web_by_tag(FakeRequest(query={"tag": "1c"}))))
    assert len(arts["articles"]) == count_1c == 3


def test_by_tag_scoped_to_project(knowledge_dir):
    _write(knowledge_dir, "testproj", "a1.md", "1c")
    _write(knowledge_dir, "general", "g1.md", "1c")
    data = _json(asyncio.run(web_by_tag(FakeRequest(query={"tag": "1c", "project": "testproj"}))))
    assert {a["file"] for a in data["articles"]} == {"a1.md"}


def test_tags_facet_scoped_to_project(knowledge_dir):
    _write(knowledge_dir, "testproj", "a1.md", "1c, deploy")
    _write(knowledge_dir, "general", "g1.md", "backup")
    all_tags = {t["tag"] for t in _json(asyncio.run(web_tags(FakeRequest())))["tags"]}
    assert {"1c", "deploy", "backup"} <= all_tags
    proj_tags = {t["tag"] for t in _json(asyncio.run(web_tags(FakeRequest(query={"project": "testproj"}))))["tags"]}
    assert "1c" in proj_tags and "deploy" in proj_tags
    assert "backup" not in proj_tags


def test_search_respects_project_filter(knowledge_dir):
    from memory_compiler.search import rebuild_index, rebuild_embeddings
    _write(knowledge_dir, "testproj", "nginx_here.md", "web", title="nginx config testproj")
    _write(knowledge_dir, "general", "nginx_other.md", "web", title="nginx config general")
    rebuild_index()
    rebuild_embeddings()
    data = _json(asyncio.run(web_search(FakeRequest(query={"q": "nginx", "project": "testproj"}))))
    projects = {r["project"] for r in data["results"]}
    assert projects <= {"testproj"}
    assert any(r["file"] == "nginx_here.md" for r in data["results"])
