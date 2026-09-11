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


# ── напоминание о заметке по ходу (v1.65.0) ─────────────────────────────────
# Замер 2026-08-26: работа после последней загрузки контекста — медиана 25 минут,
# p90 101. Инструмент, о котором надо ВСПОМНИТЬ, не работает механизмом: у
# stale_facts за 4.5 месяца ноль вызовов. Поэтому напоминает сервер, там же, где
# уже едет футер свежести.

def test_long_silent_work_gets_a_note_hint():
    a = freshness.key_for(FakeSession())
    freshness.consume(a, "infra")                       # начало работы с проектом
    freshness._seen[(a, "infra")] = time.time() - freshness.NOTE_HINT_SEC - 60
    freshness._started[(a, "infra")] = time.time() - freshness.NOTE_HINT_SEC - 60
    hint = freshness.consume(a, "infra")
    assert "session_note" in hint


def test_short_work_is_not_nagged():
    a = freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    assert "session_note" not in freshness.consume(a, "infra")


def test_hint_is_not_repeated_every_call():
    """Напоминание раз в окно, а не в каждом ответе: подсказка, повторяемая
    без конца, читается как шум и перестаёт работать."""
    a = freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    freshness._started[(a, "infra")] = time.time() - freshness.NOTE_HINT_SEC - 60
    freshness._seen[(a, "infra")] = time.time() - freshness.NOTE_HINT_SEC - 60
    assert "session_note" in freshness.consume(a, "infra")
    assert "session_note" not in freshness.consume(a, "infra")


def test_own_note_resets_the_timer():
    """Записала заметку — отсчёт пошёл заново, иначе напоминание придёт сразу
    после выполнения просьбы."""
    a = freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    freshness._started[(a, "infra")] = time.time() - freshness.NOTE_HINT_SEC - 60
    freshness.note_write("infra", "session_note", "нашёл причину", a)
    freshness._seen[(a, "infra")] = time.time() - 10
    assert "session_note" not in freshness.consume(a, "infra")


def test_note_text_is_shown_in_the_footer():
    """У заметки нет ни topic, ни filename — если брать только их, чужая сессия
    видит «session_note: (без темы)» и не понимает, что именно изменилось."""
    a, b = freshness.key_for(FakeSession()), freshness.key_for(FakeSession())
    freshness.consume(a, "infra")
    freshness.note_write("infra", "session_note", "прод отдаёт 502 после рестарта", b)
    assert "прод отдаёт 502" in freshness.consume(a, "infra")


# ── ключ по id чата клиента (v1.76.0) ───────────────────────────────────────
# Замер 11.09.2026: Claude Desktop отдаёт чатам Code серверы из
# claude_desktop_config.json через свой мост (mcp-remote), и у ВСЕХ таких чатов
# одна MCP-сессия на сервере. Ключ по объекту сессии склеивал разные чаты: новый
# чат на первом касании infra получил «25 минут без записи» от чата, работавшего
# там час назад. Mcp-Session-Id при этом никто не переиспользует (проверено
# локальным зондом), поэтому сброс по initialize не лечит. Лечит id чата, который
# клиент кладёт в аргументы вызова: аргументы едут через мост как есть, а
# заголовки у mcp-remote статические.

def _shared_bridge(monkeypatch):
    """Одна MCP-сессия на все чаты — как у моста Desktop. first_touch_context
    заменён маркером: тесту важен путь первого касания, а не содержимое базы."""
    from memory_compiler import handlers, tools

    class Ctx:
        session = FakeSession()

    class FakeApp:
        request_context = Ctx()

    monkeypatch.setattr(tools, "app", FakeApp())
    monkeypatch.setattr(handlers, "first_touch_context", lambda project: "\n\n📌 FIRST " + project)
    return tools


def _silence(key, project):
    """Ключ давно работал с проектом и молчит дольше порога подсказки."""
    old = time.time() - freshness.NOTE_HINT_SEC - 60
    freshness._seen[(key, project)] = old
    freshness._started[(key, project)] = old


def _footer(out, base):
    return "".join(b.text for b in out[len(base):])


def test_new_chat_on_shared_mcp_session_gets_its_own_key(monkeypatch):
    """Новый чат на старом ключе: чат A давно работал с infra через мост и
    замолчал, чат B открыт позже и пришёл в ту же MCP-сессию. B обязан получить
    первое касание, а не подсказку о чужом молчании."""
    from mcp.types import TextContent
    tools = _shared_bridge(monkeypatch)
    base = [TextContent(type="text", text="ответ")]

    tools._append_freshness("read_article", {"project": "infra"}, base, "chat-a")
    _silence(freshness.key_for(None, "chat-a"), "infra")

    out_b = _footer(tools._append_freshness("read_article", {"project": "infra"}, base, "chat-b"), base)
    assert "FIRST infra" in out_b, "новый чат не получил первое касание"
    assert "session_note" not in out_b, "новый чат унаследовал молчание чужого чата"

    # позитивный контроль: то же молчание у ХОЗЯИНА ключа даёт подсказку — значит,
    # у B её нет из-за отдельного ключа, а не потому что подсказка сломана
    out_a = _footer(tools._append_freshness("read_article", {"project": "infra"}, base, "chat-a"), base)
    assert "session_note" in out_a


def test_without_client_id_chats_on_one_mcp_session_share_state(monkeypatch):
    """Клиент без id чата (вкладка Chat в Desktop, чужой MCP-клиент) остаётся на
    ключе по MCP-сессии. Так выглядел баг, и так же он выглядит для клиентов,
    которые id не передают, — фиксируем явно, а не держим в голове."""
    from mcp.types import TextContent
    tools = _shared_bridge(monkeypatch)
    base = [TextContent(type="text", text="ответ")]

    tools._append_freshness("read_article", {"project": "infra"}, base)
    _silence(freshness.key_for(tools.app.request_context.session), "infra")

    out = _footer(tools._append_freshness("read_article", {"project": "infra"}, base), base)
    assert "session_note" in out and "FIRST" not in out


def test_client_session_key_survives_reconnect_and_route_switch():
    """Ключ по id чата не зависит от объекта MCP-сессии: переподключение и смена
    маршрута (свой HTTP ↔ мост Desktop) снимок не теряют."""
    chat = "0b7a1f3e-5c2d-4e8f-9a61-2d4c8e0f7b35"
    assert freshness.key_for(FakeSession(), chat) == freshness.key_for(FakeSession(), chat)
    assert freshness.key_for(FakeSession(), chat) != freshness.key_for(
        FakeSession(), "7c19e4a2-8b3f-4d05-a6e7-91f2b0c3d584")
    # и не пересекается с ключами MCP-сессий
    assert not freshness.key_for(None, chat).startswith("s")


def test_bad_client_session_falls_back_to_mcp_session():
    s = FakeSession()
    fallback = freshness.key_for(s)
    for bad in (None, "", 42, "a b", "x" * 200, "../etc", "id\n"):
        assert freshness.key_for(s, bad) == fallback, repr(bad)


def test_call_tool_strips_client_session_before_dispatch_and_audit(monkeypatch):
    """Аргумент служебный: хендлер с ним упал бы на лишнем kwarg, аудиту он не
    нужен. При этом id чата обязан дойти до freshness."""
    import asyncio
    from mcp.types import TextContent
    tools = _shared_bridge(monkeypatch)
    dispatched, audited = {}, {}

    async def fake_dispatch(name, arguments):
        dispatched.update(arguments)
        return [TextContent(type="text", text="ok")]

    monkeypatch.setattr(tools, "_dispatch_tool", fake_dispatch)
    monkeypatch.setattr(tools, "audit_log",
                        lambda name, args, size, error=None: audited.update(args))

    asyncio.run(tools.call_tool("read_article", {
        "project": "infra", "filename": "x.md", freshness.CLIENT_SESSION_ARG: "chat-a"}))

    assert dispatched == {"project": "infra", "filename": "x.md"}
    assert freshness.CLIENT_SESSION_ARG not in audited
    assert not freshness.is_first_touch(freshness.key_for(None, "chat-a"), "infra"), \
        "id чата не дошёл до freshness"
