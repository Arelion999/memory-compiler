"""Тесты переключателя cross-encoder reranker'а (v1.27.0).

Решение отключить принято ПО ЗАМЕРУ (scripts/eval_retrieval.py, 132 реальных запроса):
прироста нет, цена ×32. Тесты стерегут две вещи: дефолт не уезжает молча обратно и
при выключенном флаге модель не дёргается ВООБЩЕ (выставить бюджет в 0 было бы
недостаточно — фоновый поток всё равно досчитал бы predict вхолостую).
"""
import asyncio

import memory_compiler.handlers as H
import memory_compiler.search as S


def test_rerank_disabled_by_default():
    assert H.RERANK_ENABLED is False, "дефолт reranker'а изменён без повторного замера"


def test_rerank_async_does_not_touch_model_when_disabled(monkeypatch):
    calls = []

    def spy(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(S, "rerank", spy)
    monkeypatch.setattr(H, "RERANK_ENABLED", False)
    results = [{"file": f"{i}.md"} for i in range(5)]
    out = asyncio.run(H._rerank_async("q", results, top_k=3))
    assert out == results[:3], "при выключенном reranker'е отдаём hybrid-порядок как есть"
    assert calls == [], "reranker вызвался несмотря на RERANK_ENABLED=false"


def test_rerank_async_applies_model_when_enabled(monkeypatch):
    monkeypatch.setattr(H, "RERANK_ENABLED", True)
    monkeypatch.setattr(S, "rerank", lambda q, c, top_k: list(reversed(c))[:top_k])
    results = [{"file": "a.md"}, {"file": "b.md"}, {"file": "c.md"}]
    out = asyncio.run(H._rerank_async("q", results, top_k=2))
    assert [r["file"] for r in out] == ["c.md", "b.md"], "включённый reranker не применился"


def test_rerank_async_falls_back_on_error(monkeypatch):
    """Падение реранка не должно ронять поиск — мягкая деградация к hybrid."""
    monkeypatch.setattr(H, "RERANK_ENABLED", True)

    def boom(*a, **k):
        raise RuntimeError("model dead")

    monkeypatch.setattr(S, "rerank", boom)
    results = [{"file": "a.md"}, {"file": "b.md"}]
    out = asyncio.run(H._rerank_async("q", results, top_k=1))
    assert out == results[:1]
