"""Отложенные эмбеддинги (v1.59.0).

Замер 2026-08-26 на проде: запись 7081 мс против 275 мс у чтения, 1.1% записей
дольше 30 с (клиентский таймаут). Профиль показал виновника — encode статьи
1855 мс против 71 мс у whoosh. Вектор ушёл в фоновую очередь.
"""

import time

import pytest

from memory_compiler import embed_queue


@pytest.fixture(autouse=True)
def clean_queue(monkeypatch):
    """Своя очередь на каждый тест: состояние модульное и переживает между ними."""
    import queue as _q
    monkeypatch.setattr(embed_queue, "_q", _q.Queue())
    monkeypatch.setattr(embed_queue, "_pending", {})
    monkeypatch.setattr(embed_queue, "_worker", None)
    monkeypatch.setattr(embed_queue, "_stats", {"done": 0, "failed": 0})
    yield


def _fake_embed(monkeypatch, calls, delay=0.0, fail=False):
    def _embed(text, filename, project):
        if delay:
            time.sleep(delay)
        if fail:
            raise RuntimeError("модель недоступна")
        calls.append((project, filename, text))
    import memory_compiler.search as search
    monkeypatch.setattr(search, "embed_document", _embed)


def test_enqueue_returns_immediately_and_worker_does_the_work(monkeypatch):
    calls = []
    _fake_embed(monkeypatch, calls, delay=0.15)

    t0 = time.perf_counter()
    embed_queue.enqueue("тело статьи", "a.md", "demo")
    handed_off = (time.perf_counter() - t0) * 1000

    assert handed_off < 50, "постановка в очередь должна быть мгновенной, а не ждать модель"
    assert embed_queue.drain(timeout=10), "воркер не разгрёб очередь"
    assert calls == [("demo", "a.md", "тело статьи")]


def test_repeated_edit_replaces_job_not_piles_up(monkeypatch):
    """Промежуточные версии считать незачем — иначе очередь растёт при правках."""
    calls = []
    _fake_embed(monkeypatch, calls, delay=0.05)
    for i in range(5):
        embed_queue.enqueue("версия %d" % i, "a.md", "demo")
    assert embed_queue.drain(timeout=10)
    assert len(calls) <= 2, "каждая правка считалась отдельно: %d вызовов" % len(calls)
    assert calls[-1][2] == "версия 4", "последняя версия обязана быть посчитана"


def test_different_articles_all_processed(monkeypatch):
    calls = []
    _fake_embed(monkeypatch, calls)
    for i in range(6):
        embed_queue.enqueue("текст", "a%d.md" % i, "demo")
    assert embed_queue.drain(timeout=10)
    assert len({c[1] for c in calls}) == 6


def test_failure_does_not_kill_the_worker(monkeypatch):
    """Одна упавшая статья не должна останавливать очередь целиком."""
    calls = []
    _fake_embed(monkeypatch, calls, fail=True)
    embed_queue.enqueue("плохая", "bad.md", "demo")
    assert embed_queue.drain(timeout=10)
    assert embed_queue.counters()["failed"] == 1

    _fake_embed(monkeypatch, calls, fail=False)
    embed_queue.enqueue("хорошая", "good.md", "demo")
    assert embed_queue.drain(timeout=10)
    assert calls and calls[-1][1] == "good.md", "воркер умер после ошибки"


def test_pending_counter_reflects_backlog(monkeypatch):
    calls = []
    _fake_embed(monkeypatch, calls, delay=0.2)
    for i in range(4):
        embed_queue.enqueue("текст", "a%d.md" % i, "demo")
    assert embed_queue.pending() > 0, "счётчик очереди не показывает нагрузку"
    assert embed_queue.drain(timeout=15)
    assert embed_queue.pending() == 0


def test_worker_starts_once(monkeypatch):
    calls = []
    _fake_embed(monkeypatch, calls)
    assert embed_queue.start_worker() is True
    assert embed_queue.start_worker() is False, "второй воркер конкурировал бы за лок и pickle"


def test_missing_articles_finds_unembedded(monkeypatch, tmp_path):
    """Догон после рестарта видит статьи без единого вектора."""
    import memory_compiler.config as cfg
    import memory_compiler.search as search

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "есть.md").write_text("# есть", encoding="utf-8")
    (tmp_path / "demo" / "нет.md").write_text("# нет", encoding="utf-8")
    (tmp_path / "demo" / "_session.md").write_text("служебный", encoding="utf-8")
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PROJECTS", ["demo"])
    monkeypatch.setattr(search, "snapshot_embeddings",
                        lambda: {"demo/есть.md#0": [0.1], "demo/есть.md#1": [0.2]})

    missing = embed_queue.missing_articles()
    assert ("demo", "нет.md") in missing
    assert ("demo", "есть.md") not in missing, "статья с вектором попала в догон"
    assert all(n != "_session.md" for _, n in missing), "служебный файл не индексируется"


def test_embed_missing_queues_them(monkeypatch, tmp_path):
    import memory_compiler.config as cfg
    import memory_compiler.search as search

    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "нет.md").write_text("тело", encoding="utf-8")
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PROJECTS", ["demo"])
    monkeypatch.setattr(search, "snapshot_embeddings", lambda: {})
    calls = []
    _fake_embed(monkeypatch, calls)

    assert embed_queue.embed_missing() == 1
    assert embed_queue.drain(timeout=10)
    assert calls and calls[0][1] == "нет.md"


@pytest.mark.asyncio
async def test_handler_path_does_not_wait_for_vector(monkeypatch):
    """Сквозная проверка: запись возвращается, не дожидаясь инференса."""
    from memory_compiler import handlers

    monkeypatch.setattr(embed_queue, "ASYNC_ENABLED", True)
    monkeypatch.setattr(handlers, "index_document", lambda *a, **k: None)
    calls = []
    _fake_embed(monkeypatch, calls, delay=0.3)

    t0 = time.perf_counter()
    await handlers._index_embed("текст статьи", "a.md", "demo")
    elapsed = (time.perf_counter() - t0) * 1000

    assert elapsed < 100, "запись ждала эмбеддинг: %.0f мс" % elapsed
    assert embed_queue.drain(timeout=10)
    assert calls, "вектор так и не посчитался"


@pytest.mark.asyncio
async def test_sync_mode_still_waits(monkeypatch):
    """MC_EMBED_ASYNC=0 возвращает прежнее поведение — на нём стоят прочие тесты."""
    from memory_compiler import handlers

    monkeypatch.setattr(embed_queue, "ASYNC_ENABLED", False)
    monkeypatch.setattr(handlers, "index_document", lambda *a, **k: None)
    calls = []
    _fake_embed(monkeypatch, calls, delay=0.1)
    monkeypatch.setattr(handlers, "embed_document",
                        __import__("memory_compiler.search", fromlist=["x"]).embed_document)

    await handlers._index_embed("текст", "b.md", "demo")
    assert calls and calls[0][1] == "b.md", "синхронный режим не посчитал вектор"
