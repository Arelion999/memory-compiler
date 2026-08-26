"""Свежесть контекста между параллельными сессиями (v1.57.0).

Сценарий владельца: сессия A работает с железом, сессия B правит его же и пишет
в базу, сессия A об этом не узнаёт и разбирается с чужими изменениями с нуля.
"""

import time

import pytest

from memory_compiler import freshness


@pytest.fixture(autouse=True)
def clean():
    freshness.reset()
    yield
    freshness.reset()


class FakeSession:
    """Объект-заглушка вместо MCP-сессии: важна только идентичность."""


def test_first_touch_is_silent():
    """Первый вызов по проекту футера не даёт: данные только что получены."""
    a = freshness.key_for(FakeSession())
    assert freshness.consume(a, "infra") == ""


def test_other_session_write_is_reported():
    a, b = freshness.key_for(FakeSession()), freshness.key_for(FakeSession())
    freshness.consume(a, "infra")                      # A увидела состояние
    freshness.note_write("infra", "save_lesson", "Роутер перенастроен", b)

    note = freshness.consume(a, "infra")
    assert "другая сессия" in note
    assert "Роутер перенастроен" in note
    assert "infra" in note


def test_own_write_is_not_reported():
    """Своя запись — не новость. Иначе сессия предупреждает саму себя.

    Ровно на этом сломался клиентский вариант проверки: он сравнивал только
    время и показывал собственный finish_task как чужую правку.
    """
    a = freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    freshness.note_write("infra", "finish_task", "Мой итог", a)
    assert freshness.consume(a, "infra") == ""


def test_reported_once_not_every_call():
    a, b = freshness.key_for(FakeSession()), freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    freshness.note_write("infra", "save_lesson", "Тема", b)
    assert freshness.consume(a, "infra") != ""
    assert freshness.consume(a, "infra") == "", "футер повторяется на каждом вызове"


def test_other_project_is_not_reported():
    a, b = freshness.key_for(FakeSession()), freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    freshness.note_write("home_assistant", "save_lesson", "Чужой проект", b)
    assert freshness.consume(a, "infra") == ""


def test_project_all_falls_back_to_last_project():
    """search(project='all') не должен терять контекст проекта сессии."""
    a, b = freshness.key_for(FakeSession()), freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    freshness.note_write("infra", "edit_article", "Правка", b)
    note = freshness.consume(a, "all")
    assert "Правка" in note


def test_stale_writes_are_not_news():
    a, b = freshness.key_for(FakeSession()), freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    old = time.time() - freshness.MAX_AGE_SEC - 60
    freshness._writes.append((old, "infra", "save_lesson", "Древность", b))
    assert freshness.consume(a, "infra") == ""


def test_session_keys_are_stable_and_distinct():
    s1, s2 = FakeSession(), FakeSession()
    assert freshness.key_for(s1) == freshness.key_for(s1)
    assert freshness.key_for(s1) != freshness.key_for(s2)
    assert freshness.key_for(None) == ""


def test_many_writes_are_summarised():
    a, b = freshness.key_for(FakeSession()), freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    for i in range(9):
        freshness.note_write("infra", "save_lesson", "Тема %d" % i, b)
    note = freshness.consume(a, "infra")
    assert "и ещё 4" in note, note
    assert note.count("\n- ") == freshness.MAX_SHOWN + 1


def test_dispatcher_appends_note_only_when_there_is_news():
    """Гейт против регресса в call_tool: пустой футер не должен добавлять блок.

    414 ассертов в тестах сравнивают тексты ответов дословно — лишний
    TextContent сломал бы их молча.
    """
    from mcp.types import TextContent
    from memory_compiler import tools

    base = [TextContent(type="text", text="ответ")]

    class Ctx:
        session = FakeSession()

    class FakeApp:
        request_context = Ctx()

    real_app, tools.app = tools.app, FakeApp()
    try:
        out = tools._append_freshness("search", {"project": "infra"}, base)
        assert out == base, "первое касание не должно ничего дописывать"

        other = freshness.key_for(FakeSession())
        freshness.note_write("infra", "save_lesson", "Чужая правка", other)
        out = tools._append_freshness("search", {"project": "infra"}, base)
        assert len(out) == 2 and "Чужая правка" in out[1].text
    finally:
        tools.app = real_app


def test_dispatcher_survives_absent_request_context():
    """Вне MCP-запроса (REST, фоновые задачи) сторож обязан промолчать."""
    from mcp.types import TextContent
    from memory_compiler import tools

    base = [TextContent(type="text", text="ответ")]
    assert tools._append_freshness("search", {"project": "infra"}, base) == base
