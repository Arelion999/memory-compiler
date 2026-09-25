"""Хендлер, ждущий фоновый замок, не должен останавливать event loop.

Инцидент 25.09.2026. MCP-вызов reindex запустил rebuild_index в демон-потоке, и тот
держал _index_lock весь дисковый скан — около четырёх минут на NAS. В это время
пришёл save_lesson из соседней сессии: find_existing_article → snapshot_embeddings
ждали тот же замок ПРЯМО на loop. Сервер встал целиком: /api/health молчал три
минуты при ЦП контейнера 3%, то есть сервер не считал, а ждал. Ожил ровно тогда,
когда rebuild_index записал сегмент и отпустил замок.

Именной сторож test_no_blocking_calls этого не видел: find_existing_article в его
списке не числилась, а замок берётся двумя вызовами глубже. Класс держит
test_no_lock_waits_in_event_loop там же; здесь — поведение: соседний поток держит
настоящий замок, на loop идут хендлер и пробник /api/health, и пауза между ответами
пробника обязана оставаться короткой, пока хендлер ждёт.
"""
import asyncio
import threading
import time

import numpy as np
import pytest

HOLD_SEC = 1.5        # сколько «reindex» держит замок в соседнем потоке
STALL_MAX_SEC = 0.5   # дольше loop стоять не вправе; синхронное ожидание даст ~HOLD_SEC


class _Req:
    """Стенд под starlette.Request, как в test_mcp_sessions: web_health читает
    headers/cookies, web_related — query_params."""

    def __init__(self, query=None):
        self.headers = {}
        self.cookies = {}
        self.query_params = query or {}


def _hold(lock, held: threading.Event, seconds: float) -> None:
    with lock:
        held.set()
        time.sleep(seconds)


async def _loop_stall_while_locked(lock, handler):
    """Соседний поток держит lock HOLD_SEC секунд; на loop — handler() и пробник
    /api/health. Возвращает (самая длинная пауза между ответами пробника, ответ
    хендлера)."""
    from memory_compiler.api import web_health

    await web_health(_Req())   # индекс открыт заранее — как на работающем сервере
    held = threading.Event()
    holder = threading.Thread(target=_hold, args=(lock, held, HOLD_SEC), daemon=True)
    holder.start()
    while not held.is_set():
        await asyncio.sleep(0.005)

    gaps = []
    done = asyncio.Event()

    async def probe():
        prev = time.monotonic()
        while not done.is_set():
            await web_health(_Req())
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    task = asyncio.create_task(probe())
    await asyncio.sleep(0)     # пробник запущен раньше хендлера
    try:
        result = await handler()
    finally:
        done.set()
        await task
        holder.join()
    return max(gaps, default=0.0), result


def _write_daily_entry(knowledge_dir):
    (knowledge_dir / "daily" / "2026-09-25.md").write_text(
        "\n## Запись для compile\n\n**Время:** 2026-09-25 14:40\n**Проект:** testproj\n"
        "**Теги:** —\n\nТело записи, которое compile сверит со статьями.\n",
        encoding="utf-8")


def _cases():
    import memory_compiler.api as api
    import memory_compiler.handlers as h
    return {
        # Путь инцидента: find_existing_article и update_cross_references.
        "save_lesson": lambda: h.save_lesson(
            "Сохранение во время reindex", "тело урока", "testproj"),
        # finish_task пишет через save_lesson — клиенты зовут именно его.
        "finish_task": lambda: h.finish_task(
            "Итог во время reindex", "тело итога", "testproj", session_summary="итог"),
        "compile": lambda: h.compile(dry_run=True, project="testproj"),
        "lint": lambda: h.lint(project="testproj"),
        "web_related": lambda: api.web_related(
            _Req({"project": "testproj", "file": "test_article.md"})),
    }


@pytest.mark.parametrize("name", ["save_lesson", "finish_task", "compile", "lint",
                                  "web_related"])
def test_handler_waits_index_lock_off_loop(name, knowledge_dir, monkeypatch):
    """Пока соседний поток держит _index_lock (как rebuild_index), хендлер ждёт его
    в потоке, а loop отвечает на /api/health."""
    import memory_compiler.handlers_articles as ha
    import memory_compiler.search as sm

    # Вектор статьи считает модель — медленно и к делу не относится; ждёт ли
    # индексация замок, решает index_document, он остаётся настоящим.
    monkeypatch.setattr(ha, "embed_document", lambda *a, **k: None)
    _write_daily_entry(knowledge_dir)

    stall, result = asyncio.run(_loop_stall_while_locked(sm._index_lock, _cases()[name]))

    assert stall < STALL_MAX_SEC, (
        f"{name}: event loop стоял {stall:.2f} с, пока соседний поток держал "
        f"_index_lock {HOLD_SEC} с — хендлер ждёт замок прямо на loop, а не в потоке")
    assert result, f"{name}: хендлер не вернул ответ"


def _new_articles(proj):
    return {p.name for p in proj.glob("*.md") if not p.name.startswith("_")} - {"test_article.md"}


def test_retry_while_waiting_lock_does_not_duplicate_article(knowledge_dir, monkeypatch):
    """Повтор той же записи, пока первая ждёт замок, не плодит статью-двойник.

    Reindex держит замок минутами, клиент не дожидается ответа и повторяет запись.
    Оба поиска ждут в потоке и возвращаются вместе. Реши «новая или мёрж» по находке,
    сделанной ДО ожидания, — и рядом с slug.md ляжет slug_<дата>.md с тем же текстом."""
    import memory_compiler.handlers as h
    import memory_compiler.handlers_articles as ha
    import memory_compiler.search as sm
    monkeypatch.setattr(ha, "embed_document", lambda *a, **k: None)
    sm.get_index()   # индекс открыт заранее — как на работающем сервере

    async def body():
        held = threading.Event()
        holder = threading.Thread(target=_hold, args=(sm._index_lock, held, 0.5), daemon=True)
        holder.start()
        while not held.is_set():
            await asyncio.sleep(0.005)
        await asyncio.gather(h.save_lesson("Повтор записи", "то же тело", "testproj"),
                             h.save_lesson("Повтор записи", "то же тело", "testproj"))
        holder.join()

    asyncio.run(body())
    new = _new_articles(knowledge_dir / "testproj")
    assert len(new) == 1, f"повтор записи создал двойника: {sorted(new)}"


def test_merge_target_gone_while_waiting_goes_to_new_article(knowledge_dir, monkeypatch):
    """Цель мёржа удалили, пока поиск ждал замок: запись ложится в новую статью, а не
    падает на чтении пропавшего файла (урок остался бы только в дневном логе)."""
    import memory_compiler.handlers as h
    import memory_compiler.handlers_articles as ha
    gone = knowledge_dir / "testproj" / "gone.md"      # найдена, но файла уже нет
    monkeypatch.setattr(ha, "embed_document", lambda *a, **k: None)
    monkeypatch.setattr(ha, "find_existing_article", lambda *a, **k: gone)

    result = asyncio.run(h.save_lesson("Запись после удаления", "тело", "testproj"))

    assert "Создано" in result[0].text, result[0].text
    assert len(_new_articles(knowledge_dir / "testproj")) == 1


def test_knowledge_gap_waits_model_load_off_loop(knowledge_dir, monkeypatch):
    """Тот же класс со вторым замком: прогрев на старте держит _model_load_lock, пока
    грузит модель (минуты на NAS). knowledge_gap звал get_embed_model прямо на loop."""
    import memory_compiler.handlers as h
    import memory_compiler.search as sm

    class FakeModel:
        max_seq_length = 128

        def __init__(self, name):
            pass

        def encode(self, texts, **kw):
            return np.array([[1.0, 0.0]] * len(texts))

    monkeypatch.setattr(sm, "SentenceTransformer", FakeModel)
    monkeypatch.setattr(sm, "_embed_model", None)            # модель ещё грузится
    monkeypatch.setattr(sm, "_embeddings", {"testproj/test_article.md": np.array([1.0, 0.0])})
    raw = "abcdef1|feat: проверка reindex при сохранении|dev|2026-09-25T14:40:00+10:00\n"

    stall, result = asyncio.run(_loop_stall_while_locked(
        sm._model_load_lock, lambda: h.knowledge_gap(git_log_raw=raw, project="testproj")))

    assert stall < STALL_MAX_SEC, (
        f"knowledge_gap: event loop стоял {stall:.2f} с, пока соседний поток держал "
        f"_model_load_lock {HOLD_SEC} с — get_embed_model зовётся прямо на loop")
    assert "Knowledge Gap Report" in result[0].text, result[0].text


def test_slug_twin_created_while_waiting_wins_over_semantic_hit(knowledge_dir, monkeypatch):
    """Пока поиск ждал замок, появилась статья с тем же слагом: запись идёт в неё, а не в
    семантического соседа из потока. У find_existing_article слаг — первая ступень, и
    перепроверка на loop обязана держать тот же порядок, иначе одна тема разъедется
    по двум статьям."""
    import memory_compiler.handlers as h
    import memory_compiler.handlers_articles as ha
    from memory_compiler.storage import make_slug
    proj = knowledge_dir / "testproj"
    topic = "Слаг появился во время ожидания"
    neighbour = proj / "neighbour.md"          # семантическая находка потока
    neighbour.write_text("# Сосед\n\n## Записи\n\n### 2026-09-25 10:00\nстарое\n",
                         encoding="utf-8")
    twin = proj / f"{make_slug(topic)}.md"     # создана, пока поток ждал замок

    def found_after_wait(*a, **k):
        twin.write_text(f"# {topic}\n\n## Записи\n\n### 2026-09-25 10:01\nпервая\n",
                        encoding="utf-8")
        return neighbour

    monkeypatch.setattr(ha, "embed_document", lambda *a, **k: None)
    monkeypatch.setattr(ha, "find_existing_article", found_after_wait)

    asyncio.run(h.save_lesson(topic, "вторая запись той же темы", "testproj"))

    assert "вторая запись той же темы" in twin.read_text(encoding="utf-8")
    assert "вторая запись той же темы" not in neighbour.read_text(encoding="utf-8")
