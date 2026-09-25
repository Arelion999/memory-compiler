"""Повторный start_task в том же чате — следом (v1.92.0).

Замер 25.09.2026 по транскриптам за 14 дней: 101 повтор в том же чате и проекте из
310 вызовов; в повторах в пределах 3 ч 36% текста уже было в прошлой выдаче —
открытые вопросы 49% повторённого, «Найдено» 19%, «Связанные действия» 14%.
"""
import pytest

from memory_compiler import freshness
from memory_compiler import handlers_sessions as hs


@pytest.fixture(autouse=True)
def clean_freshness():
    freshness.reset()
    yield
    freshness.reset()


# ── след и метки ────────────────────────────────────────────────────────────

def test_trace_is_short_and_marked():
    item = "- **2026-09-24 18:49** — " + "вопрос " * 40
    t = hs._trace(item)
    assert t.startswith("- **2026-09-24 18:49** — вопрос")
    assert t.endswith(hs.TRACE_MARK)
    assert len(t) <= hs.TRACE_CHARS + len("…") + len(hs.TRACE_MARK)


def test_trace_joins_lines_of_multiline_item():
    t = hs._trace("### [demo] Заголовок\nстрока тела 1\n\nстрока тела 2")
    assert t == "### [demo] Заголовок · строка тела 1 · строка тела 2 ↺"


def test_trace_pointer_names_window_and_tools():
    p = hs._trace_pointer()
    assert p.startswith("*↺ — уже было в этом чате за последние 3 ч")
    for tool in ("open_questions", "load_session", "read_article"):
        assert f"`{tool}`" in p


def test_block_gets_text_idents_by_default_and_drops_empty_items():
    b = hs._Block("questions", "## В", ["- а", "", "- б"], weight=1.0)
    assert b.items == ["- а", "- б"]
    assert b.idents == [hs._txt_id("- а"), hs._txt_id("- б")]
    assert b.idents[0] != b.idents[1]
    assert b.idents[0].startswith("txt:") and len(b.idents[0]) == len("txt:") + 16


def test_block_keeps_explicit_idents_in_step_with_items():
    b = hs._Block("found", "## Н", ["### a", "", "### b"], weight=1.0,
                  idents=["art:p/a.md", "art:p/x.md", "art:p/b.md"])
    assert b.items == ["### a", "### b"]
    assert b.idents == ["art:p/a.md", "art:p/b.md"]


def test_block_rejects_idents_of_other_length():
    with pytest.raises(ValueError):
        hs._Block("facts", "## Ф", ["- а", "- б"], weight=1.0, idents=["m1"])


def test_render_block_reports_only_fully_shown_items():
    items = ["- " + "а" * 50, "- " + "б" * 50, "- " + "в" * 50]
    b = hs._Block("facts", "## Ф", items, weight=1.0, idents=["m1", None, "m3"])
    report = []
    # заголовок 4 + два пункта по 53 (52 + разделитель) + запас 5: третий не влезает
    text = hs._render_block(b, len("## Ф") + 2 * 53 + 5, report)
    assert "б" * 50 in text and "в" * 50 not in text
    assert report == ["m1", None], "в отчёт попал не показанный пункт или пропал след"


def test_render_block_does_not_report_cut_tail():
    b = hs._Block("session", "## С", ["я" * 300], weight=1.0, idents=["m1"])
    report = []
    text = hs._render_block(b, len("## С") + 200, report)   # влезает только кусок
    assert text and "я" * 300 not in text
    assert report == [], "подрезанный пункт записан как показанный целиком"


def test_render_block_without_report_works_as_before():
    b = hs._Block("facts", "## Ф", ["- а"], weight=1.0)
    assert hs._render_block(b, 100) == "## Ф\n- а"


# ── start_task: повтор в том же чате ─────────────────────────────────────────
import types

from memory_compiler import handlers, storage

Q1 = ("Какой брокер ставить на NAS — mosquitto или emqx, и кто будет обновлять его "
      "после переезда стойки в новый шкаф?")
Q2 = "Нужен ли брокеру отдельный том под журнал сообщений?"


@pytest.fixture
def demo(knowledge_dir, monkeypatch):
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "PROJECTS", ["demo", "demo2"])
    (knowledge_dir / "demo").mkdir()
    (knowledge_dir / "demo2").mkdir()
    return knowledge_dir / "demo"


def _fake(monkeypatch, hits):
    async def fake(query, project="all", limit=20):
        return [dict(h) for h in hits]
    monkeypatch.setattr(handlers, "_whoosh_async", fake)


HIT = {"project": "demo", "file": "broker.md", "title": "Выбор брокера сообщений", "score": 90,
       "preview": "# Выбор брокера сообщений\nстрока тела 1\nстрока тела 2\nстрока тела 3\nстрока тела 4"}


async def _start(topic="брокер сообщений", key="c:chat-a", project="demo"):
    token = freshness.chat_key_var.set(key)
    try:
        return (await handlers.start_task(topic, project))[0].text
    finally:
        freshness.chat_key_var.reset(token)


@pytest.mark.asyncio
async def test_repeat_in_same_chat_shows_traces_and_pointer(demo, monkeypatch):
    storage.add_question("demo", Q1)
    _fake(monkeypatch, [HIT])
    first = await _start()
    second = await _start()
    assert Q1 in first and hs.TRACE_MARK not in first and hs._trace_pointer() not in first
    assert Q1 not in second, "вопрос повторён целиком"
    assert Q1[:40] in second, "от вопроса не осталось следа"
    assert ("### [demo] Выбор брокера сообщений · строка тела 1 · строка тела 2 · "
            "строка тела 3 · строка тела 4 ↺") in second
    assert hs._trace_pointer() in second
    assert second.index(hs._trace_pointer()) < second.index("## "), "указатель не под заголовком"


@pytest.mark.asyncio
async def test_new_question_is_shown_in_full_next_to_trace(demo, monkeypatch):
    _fake(monkeypatch, [])
    storage.add_question("demo", Q1)
    await _start()
    storage.add_question("demo", Q2)
    second = await _start()
    assert Q2 in second, "новый вопрос не показан целиком"
    assert Q1 not in second and Q1[:40] in second


@pytest.mark.asyncio
async def test_changed_session_block_is_shown_in_full(demo, monkeypatch):
    _fake(monkeypatch, [])
    storage.append_session("demo", "Первая сводка: брокер сообщений поднят на NAS")
    first = await _start()
    # Длиннее следа (100 символов): хвост виден, только если блок показан целиком.
    storage.append_session("demo", "Вторая сводка: брокер сообщений переехал в Docker, "
                                   + "подробности переезда " * 6 + "и хвост второй сводки")
    second = await _start()
    assert "Первая сводка" in first
    assert "и хвост второй сводки" in second, "изменившийся блок журнала показан следом"


@pytest.mark.asyncio
async def test_without_chat_key_output_is_unchanged(demo, monkeypatch):
    storage.add_question("demo", Q1)
    _fake(monkeypatch, [HIT])
    first = await _start(key="")
    second = await _start(key="")
    assert second == first
    assert hs.TRACE_MARK not in second
    assert freshness._shown == {}, "без ключа чата память заведена"


@pytest.mark.asyncio
async def test_full_text_returns_after_window(demo, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(freshness, "time", types.SimpleNamespace(time=lambda: clock[0]))
    _fake(monkeypatch, [])
    storage.add_question("demo", Q1)
    await _start()
    clock[0] += freshness.SHOWN_WINDOW_SEC + 1
    later = await _start()
    assert Q1 in later, "через окно вопрос всё ещё следом"


@pytest.mark.asyncio
async def test_trace_does_not_extend_window(demo, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(freshness, "time", types.SimpleNamespace(time=lambda: clock[0]))
    _fake(monkeypatch, [])
    storage.add_question("demo", Q1)
    first = await _start()
    clock[0] += freshness.SHOWN_WINDOW_SEC / 2
    second = await _start()
    clock[0] += freshness.SHOWN_WINDOW_SEC / 2 + 1
    third = await _start()
    assert Q1 in first and Q1 not in second
    assert Q1 in third, "след продлил окно: полный текст не вернулся через окно от полного показа"


@pytest.mark.asyncio
async def test_item_hidden_by_budget_comes_in_full_next_time(demo, monkeypatch):
    # 5 вопросов по 181 символу с разделителем, заголовок 26: при 720 первая выдача
    # показывает 3 и прячет 2; во второй 3 следа (~104) освобождают место для обоих.
    monkeypatch.setattr(hs, "START_BUDGET", 720)
    _fake(monkeypatch, [])
    qs = [f"Вопрос номер {i}: " + "подробность " * 11 + f"конец {i}" for i in range(5)]
    for q in qs:
        storage.add_question("demo", q)
    first = await _start()
    hidden = [q for q in qs if q not in first]
    assert hidden, "бюджет теста не скрыл ни одного вопроса — поправь START_BUDGET"
    second = await _start()
    assert all(q in second for q in hidden), "скрытый бюджетом вопрос не пришёл целиком"


@pytest.mark.asyncio
async def test_other_project_and_other_chat_are_full(demo, monkeypatch):
    _fake(monkeypatch, [])
    storage.add_question("demo", Q1)
    storage.add_question("demo2", Q1)
    await _start()
    assert Q1 in await _start(project="demo2"), "память одного проекта спрятала другой"
    assert Q1 in await _start(key="c:chat-b"), "память одного чата спрятала другой"


@pytest.mark.asyncio
async def test_pointer_above_empty_found_banner_and_first_heading(demo, monkeypatch):
    _fake(monkeypatch, [])
    storage.add_question("demo", Q1)
    await _start()
    second = await _start()
    assert (second.index(hs._trace_pointer())
            < second.index("*Похожих кейсов не найдено в базе.*")
            < second.index("## ")), "указатель следа не под заголовком «# Контекст для:»"
