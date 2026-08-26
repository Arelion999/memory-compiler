"""Отложенные эмбеддинги: клиент не ждёт инференс модели.

⚠️ ЗАЧЕМ. Замер 2026-08-26 на боевом сервере (структурный лог, 1720 вызовов):
чтения — медиана 275 мс, записи — 7081 мс, p90 13.5 с, максимум 183 с, и 1.1%
записей перешагивают 30 секунд, попадая в клиентский таймаут MCP. Профиль записи
в работающем контейнере показал, где время:

    git add -A (обход дерева)        36 мс
    find_existing_article (дубли)     4 мс
    whoosh_search                    71 мс
    encode статьи 3000 символов    1855 мс   ← всё здесь

То есть виноват не git и не поиск дублей (прежние гипотезы), а кодирование
текста моделью на CPU без ускорителя. Индексация и раньше уходила в отдельный
поток и сервер не морозила — но вызов не возвращался, пока она не закончится,
поэтому ждал КЛИЕНТ.

Разделение простое: текстовый индекс (whoosh, 71 мс) остаётся синхронным, и
статья находится поиском сразу; вектор считается фоном. Между сохранением и
появлением статьи в семантическом поиске образуется окно в несколько секунд —
осознанная плата за отзывчивость.

⚠️ ОЧЕРЕДЬ В ПАМЯТИ, ПОЭТОМУ НУЖЕН ДОГОН. Контейнер перезапускается часто
(mc-watcher, десятки раз в день при активной разработке), и всё, что не успело
посчитаться, теряется молча. `embed_missing()` находит статьи без единого
вектора и досчитывает их; вызывается на старте сервера после прогрева модели.

⚠️ ОДИН ВОРКЕР, НЕ ПУЛ. `embed_document` берёт `_index_lock` и пишет pickle
целиком; параллельные воркеры дрались бы за лок и переписывали файл поверх друг
друга, не ускорив ничего — модель всё равно одна и CPU общий.
"""

from __future__ import annotations

import os
import queue
import threading
import time

from memory_compiler import obs

# Синхронный режим: тесты и отладка ждут вектор сразу. Прод — асинхронный.
ASYNC_ENABLED = os.environ.get("MC_EMBED_ASYNC", "1") not in ("0", "false", "False")

_q: "queue.Queue[tuple[str, str, str] | None]" = queue.Queue()
_pending: dict[str, tuple[str, str, str]] = {}   # parent_key -> задание (последнее)
_lock = threading.Lock()
_worker: threading.Thread | None = None
_stats = {"done": 0, "failed": 0}


def _key(project: str, filename: str) -> str:
    return f"{project}/{filename}"


def enqueue(text: str, filename: str, project: str) -> None:
    """Поставить статью в очередь на векторизацию.

    Повторная правка той же статьи ЗАМЕЩАЕТ прежнее задание: считать промежуточную
    версию незачем, а при активном редактировании очередь иначе растёт линейно.
    """
    k = _key(project, filename)
    with _lock:
        fresh = k not in _pending
        _pending[k] = (text, filename, project)
    if fresh:
        _q.put(k)
    start_worker()


def pending() -> int:
    with _lock:
        return len(_pending)


def counters() -> dict:
    with _lock:
        return {"pending": len(_pending), **_stats}


def _run() -> None:
    log = obs.get_logger("embed")
    while True:
        k = _q.get()
        if k is None:                      # сигнал остановки
            _q.task_done()
            return
        with _lock:
            job = _pending.get(k)
        if job is None:                    # уже посчитано другим проходом
            _q.task_done()
            continue
        text, filename, project = job
        t0 = time.perf_counter()
        try:
            from memory_compiler.search import embed_document
            embed_document(text, filename, project)
            with _lock:
                _stats["done"] += 1
            log.info("embedded", extra={"article": k,
                                        "dur_ms": int((time.perf_counter() - t0) * 1000),
                                        "pending": pending() - 1})
        except Exception as e:
            with _lock:
                _stats["failed"] += 1
            # Молча ронять статью нельзя: она останется без вектора и не найдётся
            # семантикой, а внешне сохранение выглядело успешным.
            log.error(f"embed failed: {e}", extra={"article": k, "err_code": type(e).__name__})
        finally:
            with _lock:
                if _pending.get(k) == job:   # не затираем более свежую правку
                    _pending.pop(k, None)
            _q.task_done()


def start_worker() -> bool:
    """Поднять воркер, если он ещё не работает. Идемпотентно."""
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return False
        _worker = threading.Thread(target=_run, name="embed-queue", daemon=True)
        _worker.start()
        return True


def drain(timeout: float = 60.0) -> bool:
    """Дождаться опустошения очереди. Для тестов и остановки сервера."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pending() == 0:
            return True
        time.sleep(0.05)
    return pending() == 0


def missing_articles() -> list[tuple[str, str]]:
    """Статьи на диске, у которых нет ни одного вектора."""
    from memory_compiler.config import KNOWLEDGE_DIR, PROJECTS
    from memory_compiler.search import snapshot_embeddings

    keys = snapshot_embeddings().keys()
    have = {k.split("#", 1)[0] for k in keys}
    out = []
    for proj in PROJECTS:
        pdir = KNOWLEDGE_DIR / proj
        if not pdir.is_dir():
            continue
        for md in pdir.glob("*.md"):
            if md.name.startswith("_"):
                continue
            if f"{proj}/{md.name}" not in have:
                out.append((proj, md.name))
    return out


def embed_missing(limit: int = 200) -> int:
    """Догон после рестарта: поставить в очередь статьи без вектора.

    Возвращает число поставленных. Лимит нужен, чтобы догон на пустом индексе не
    превратился в полную пересборку — для неё есть rebuild_embeddings.
    """
    from memory_compiler.config import KNOWLEDGE_DIR

    missing = missing_articles()
    if not missing:
        return 0
    log = obs.get_logger("embed")
    log.info("embed backlog found", extra={"count": len(missing), "limit": limit})
    n = 0
    for proj, name in missing[:limit]:
        try:
            text = (KNOWLEDGE_DIR / proj / name).read_text(encoding="utf-8")
        except Exception:
            continue
        enqueue(text, name, proj)
        n += 1
    return n
