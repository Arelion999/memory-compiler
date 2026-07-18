"""Тесты related-notes (v1.22.1+): выбор семантически близких статей и endpoint сайдбара.

Логика проверяется на ИНЪЕКТИРОВАННЫХ эмбеддингах, а не на живой модели: выдача
становится детерминированной, тесты не тянут sentence-transformers и проходят
офлайн. Стерегут: исключение самой статьи (включая все её #chunkN), схлопывание
чанк→статья по max, сортировку по убыванию, лимит и устойчивость endpoint'а к
эмбеддингам удалённых статей.
"""
import asyncio
import json

import numpy as np

from memory_compiler import search as _smod
from memory_compiler.search import related_articles
from memory_compiler.api import web_related


class FakeRequest:
    """Минимальный stand-in под starlette.Request для прямого вызова endpoint'ов."""

    def __init__(self, query=None, path=None):
        self.query_params = query or {}
        self.path_params = path or {}


def _json(resp):
    return json.loads(resp.body)


def _v(*xs):
    """Нормализованный вектор — эмбеддинги в проде тоже нормализованы (dot = косинус)."""
    a = np.array(xs, dtype=float)
    return a / np.linalg.norm(a)


def _write(kd, project, name, title="T"):
    p = kd / project
    p.mkdir(exist_ok=True)
    (p / name).write_text(
        f"# {title}\n\n**Дата:** 2026-01-01 10:00\n**Проект:** {project}\n"
        f"**Теги:** test\n\n## Записи\nтело статьи\n",
        encoding="utf-8",
    )


# ─── related_articles ────────────────────────────────────────────────────────

def test_related_ranks_by_similarity_and_excludes_self(monkeypatch):
    monkeypatch.setattr(_smod, "_embeddings", {
        "p/a.md": _v(1, 0, 0),
        "p/near.md": _v(1, 0.2, 0),
        "p/far.md": _v(0, 1, 0),
    })
    res = related_articles("p/a.md")
    assert [k for k, _ in res] == ["p/near.md", "p/far.md"], "сортировка по убыванию сходства"
    assert all(k != "p/a.md" for k, _ in res), "сама статья попала в свои же соседи"
    assert res[0][1] > res[1][1]
    assert 0.0 <= res[1][1] <= 1.0


def test_related_dedups_chunks_to_article_taking_max(monkeypatch):
    """У статьи-кандидата несколько чанков — берётся ЛУЧШИЙ, статья одна в выдаче."""
    monkeypatch.setattr(_smod, "_embeddings", {
        "p/a.md": _v(1, 0, 0),
        "p/multi.md#chunk0": _v(0, 1, 0),      # далёкий чанк
        "p/multi.md#chunk1": _v(1, 0.1, 0),    # близкий чанк
    })
    res = related_articles("p/a.md")
    assert [k for k, _ in res] == ["p/multi.md"], "чанки не схлопнулись в одну статью"
    assert res[0][1] > 0.9, "взят не максимальный по чанкам скор"


def test_related_excludes_every_chunk_of_target(monkeypatch):
    """Цель задана без #chunk, но ВСЕ её чанки должны быть исключены из соседей."""
    monkeypatch.setattr(_smod, "_embeddings", {
        "p/a.md#chunk0": _v(1, 0, 0),
        "p/a.md#chunk1": _v(0, 1, 0),
        "p/b.md": _v(1, 0, 0),
    })
    res = related_articles("p/a.md")
    assert [k for k, _ in res] == ["p/b.md"]


def test_related_uses_best_chunk_of_target(monkeypatch):
    """Сходство считается по ЛУЧШЕЙ паре (чанк цели × чанк кандидата)."""
    monkeypatch.setattr(_smod, "_embeddings", {
        "p/a.md#chunk0": _v(1, 0, 0),
        "p/a.md#chunk1": _v(0, 0, 1),
        "p/b.md": _v(0, 0, 1),   # совпадает со ВТОРЫМ чанком цели
    })
    res = related_articles("p/a.md")
    assert res[0][0] == "p/b.md"
    assert res[0][1] > 0.99, "второй чанк цели не участвовал в сравнении"


def test_related_respects_limit(monkeypatch):
    monkeypatch.setattr(_smod, "_embeddings", {
        "p/a.md": _v(1, 0, 0),
        **{f"p/n{i}.md": _v(1, i / 100, 0) for i in range(10)},
    })
    assert len(related_articles("p/a.md", limit=3)) == 3


def test_related_empty_when_target_has_no_embedding(monkeypatch):
    monkeypatch.setattr(_smod, "_embeddings", {"p/other.md": _v(1, 0, 0)})
    assert related_articles("p/missing.md") == []


def test_related_empty_when_no_embeddings_at_all(monkeypatch):
    monkeypatch.setattr(_smod, "_embeddings", {})
    assert related_articles("p/a.md") == []


# ─── /api/related ────────────────────────────────────────────────────────────

def test_web_related_returns_title_and_score(knowledge_dir, monkeypatch):
    _write(knowledge_dir, "testproj", "a.md", title="Целевая")
    _write(knowledge_dir, "testproj", "near.md", title="Близкая статья")
    monkeypatch.setattr(_smod, "_embeddings", {
        "testproj/a.md": _v(1, 0, 0),
        "testproj/near.md": _v(1, 0.1, 0),
    })
    resp = asyncio.run(web_related(FakeRequest(query={"project": "testproj", "file": "a.md"})))
    data = _json(resp)["related"]
    assert len(data) == 1
    assert data[0]["project"] == "testproj"
    assert data[0]["file"] == "near.md"
    assert data[0]["title"] == "Близкая статья", "заголовок берётся из тела статьи"
    assert 0.0 <= data[0]["score"] <= 1.0


def test_web_related_skips_deleted_articles(knowledge_dir, monkeypatch):
    """Эмбеддинг пережил удаление статьи (до пересборки) — endpoint не должен падать."""
    _write(knowledge_dir, "testproj", "a.md")
    monkeypatch.setattr(_smod, "_embeddings", {
        "testproj/a.md": _v(1, 0, 0),
        "testproj/ghost.md": _v(1, 0.1, 0),   # файла на диске нет
    })
    resp = asyncio.run(web_related(FakeRequest(query={"project": "testproj", "file": "a.md"})))
    assert _json(resp)["related"] == []


def test_web_related_requires_params():
    assert _json(asyncio.run(web_related(FakeRequest(query={}))))["related"] == []
    assert _json(asyncio.run(web_related(FakeRequest(query={"project": "p"}))))["related"] == []


def test_web_related_rejects_traversal():
    resp = asyncio.run(web_related(FakeRequest(query={"project": "testproj", "file": "../../etc/passwd"})))
    assert resp.status_code == 404


def test_web_related_clamps_limit(knowledge_dir, monkeypatch):
    """limit ограничен сверху, мусорное значение не роняет endpoint."""
    _write(knowledge_dir, "testproj", "a.md")
    for i in range(30):
        _write(knowledge_dir, "testproj", f"n{i}.md", title=f"N{i}")
    monkeypatch.setattr(_smod, "_embeddings", {
        "testproj/a.md": _v(1, 0, 0),
        **{f"testproj/n{i}.md": _v(1, i / 100, 0) for i in range(30)},
    })
    q = {"project": "testproj", "file": "a.md", "limit": "999"}
    assert len(_json(asyncio.run(web_related(FakeRequest(query=q))))["related"]) == 25
    q["limit"] = "abc"
    assert len(_json(asyncio.run(web_related(FakeRequest(query=q))))["related"]) == 8
