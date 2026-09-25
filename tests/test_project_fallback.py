"""Проект берётся из истории сессии, когда вызов его не назвал (v1.90.0).

Замер 20.09.2026 по транскриптам: 78 отказов `-32602` по инструментам базы за 30
дней, и самая крупная их часть — пропущенный `project`:

    finish_task.project  21    save_lesson.project  7
    session_note.project  2    save_session.project 1

Это тот же корень, что у всего класса: клиент не показывает модели `required` для
строковых параметров, она читает поле как опциональное и опускает. Маркер
« (обязательно)» в описании (v1.54.0) снизил частоту, но не убрал — падения идут
и в сентябре 2026.

Дописывать в описание больше нечего, зато сервер САМ знает ответ: `freshness`
держит последний проект каждой сессии, чтобы отвечать на вопрос «чья это
запись». Тот же снимок отвечает и на «куда писать», а запись, которая иначе
потерялась бы целиком, доезжает. Куда она легла, ответ называет вслух — молча
подставленный проект был бы хуже отказа.
"""

import asyncio

import pytest
from mcp.types import TextContent

from memory_compiler import freshness, tools


class FakeSession:
    """Объект-заглушка вместо MCP-сессии: важна только идентичность."""


@pytest.fixture
def bridge(monkeypatch, knowledge_dir):
    """Одна MCP-сессия на все чаты — как у моста Claude Desktop."""
    from memory_compiler import handlers
    # Проект, с которым работает сессия, обязан существовать: чтение
    # несуществующего получает подсказку и в историю сессии не попадает.
    (knowledge_dir / "infra").mkdir()

    class Ctx:
        session = FakeSession()

    class FakeApp:
        request_context = Ctx()

    monkeypatch.setattr(tools, "app", FakeApp())
    monkeypatch.setattr(handlers, "first_touch_context", lambda project: "")
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    freshness.reset()
    seen = {}

    async def fake_dispatch(name, arguments):
        seen.clear()
        seen.update(arguments)
        return [TextContent(type="text", text="✅ записано")]

    monkeypatch.setattr(tools, "_dispatch_tool", fake_dispatch)
    return seen


def _call(name, args, chat="chat-a"):
    return asyncio.run(tools.call_tool(
        name, {**args, freshness.CLIENT_SESSION_ARG: chat}))


def test_missing_project_falls_back_to_the_one_this_chat_worked_with(bridge):
    _call("search", {"query": "роутер", "project": "infra"})
    _call("finish_task", {"topic": "итог", "content": "текст"})
    assert bridge.get("project") == "infra", (
        "запись без project обязана уйти в проект этой сессии: %r" % bridge)


def test_substituted_project_is_named_in_the_answer(bridge):
    _call("search", {"query": "роутер", "project": "infra"})
    out = _call("finish_task", {"topic": "итог", "content": "текст"})
    text = "".join(c.text for c in out if isinstance(c, TextContent))
    assert "infra" in text, "подставленный проект обязан быть назван: %r" % text


def test_explicit_project_is_never_overridden(bridge):
    """Позитивный контроль: подстановка не должна перехватывать явный выбор."""
    _call("search", {"query": "роутер", "project": "infra"})
    _call("save_lesson", {"topic": "t", "content": "c", "project": "general"})
    assert bridge.get("project") == "general"


def test_chat_without_history_gets_an_error_naming_the_parameter(bridge):
    """Подставить нечего — отказ обязан объяснить, чего не хватило, и не звать
    хендлер: тот упал бы на отсутствующем обязательном аргументе."""
    out = _call("finish_task", {"topic": "итог", "content": "текст"}, chat="chat-new")
    text = "".join(c.text for c in out if isinstance(c, TextContent))
    assert "project" in text and not bridge, text


def test_reading_tools_are_left_alone(bridge):
    """Поиску `project` не нужен: там 'all' — осмысленный режим «по всей базе»,
    и подстановка молча сузила бы выдачу до одного проекта."""
    _call("search", {"query": "роутер", "project": "infra"})
    _call("search", {"query": "другой вопрос"})
    assert "project" not in bridge, bridge


# ── чтение чужого проекта не перехватывает проект сессии (24.09.2026) ────────
# Снимок свежести обновлял «последний проект» на КАЖДОМ вызове с project, в том
# числе на чистом чтении. Сессия работает с A, для справки читает статью из B —
# и запись без project уходит в B. Ответ называет подставленный проект, но запись
# к этому моменту уже легла не туда. Проект, в который сессия писала или по
# которому звала start_task, чтением не перебивается.

def test_reading_another_project_keeps_the_one_the_chat_writes_to(bridge):
    _call("save_lesson", {"topic": "t", "content": "c", "project": "infra"})
    _call("read_article", {"project": "general", "filename": "справка.md"})
    _call("finish_task", {"topic": "итог", "content": "текст"})
    assert bridge.get("project") == "infra", (
        "чтение чужой статьи увело запись из рабочего проекта: %r" % bridge)


def test_start_task_marks_the_working_project(bridge):
    _call("start_task", {"topic": "роутер", "project": "infra"})
    _call("search", {"query": "справка", "project": "general"})
    _call("session_note", {"note": "выяснилось"})
    assert bridge.get("project") == "infra", (
        "поиск по чужому проекту увёл заметку из проекта задачи: %r" % bridge)


def test_start_task_over_the_whole_base_does_not_become_the_project(bridge):
    """start_task(project='all') — поиск по всей базе, а не выбор проекта: запись
    без project не должна уехать в несуществующий проект «all»."""
    _call("save_lesson", {"topic": "t", "content": "c", "project": "infra"})
    _call("start_task", {"topic": "роутер", "project": "all"})
    _call("finish_task", {"topic": "итог", "content": "текст"})
    assert bridge.get("project") == "infra", bridge


def test_writing_into_another_project_moves_the_working_project(bridge):
    """Позитивный контроль: рабочий проект не залипает на первом. Явная запись в
    другой проект — это работа с ним, и следующая запись без project идёт туда."""
    _call("save_lesson", {"topic": "t", "content": "c", "project": "infra"})
    _call("save_lesson", {"topic": "t2", "content": "c2", "project": "general"})
    _call("finish_task", {"topic": "итог", "content": "текст"})
    assert bridge.get("project") == "general", bridge


def test_working_project_survives_container_restart(bridge, tmp_path, monkeypatch):
    """Рестарт контейнера (watcher — десятки раз в день) не сбрасывает рабочий проект:
    чат писал в infra, после рестарта прочёл general — запись без project идёт в infra."""
    monkeypatch.setattr(freshness, "STATE_PATH", tmp_path / "freshness.json", raising=False)
    _call("save_lesson", {"topic": "t", "content": "c", "project": "infra"})
    freshness.reset()                                            # рестарт контейнера
    _call("read_article", {"project": "general", "filename": "справка.md"})
    _call("finish_task", {"topic": "итог", "content": "текст"})
    assert bridge.get("project") == "infra", (
        "после рестарта запись ушла в прочитанный проект: %r" % bridge)


def test_chat_that_only_reads_keeps_the_last_read(bridge):
    """Пока сессия ничего не писала и не звала start_task, сильнее последнего
    чтения сигнала нет: поведение v1.90.0 для таких сессий не меняется."""
    _call("search", {"query": "роутер", "project": "infra"})
    _call("search", {"query": "справка", "project": "general"})
    _call("finish_task", {"topic": "итог", "content": "текст"})
    assert bridge.get("project") == "general", bridge


# ── save_tracking принимает прозаический отчёт (v1.90.0) ────────────────────
# Живой случай 19.09.2026: суточная проверка узла ушла строкой в `facts`, где
# схема ждёт объект. Клиент отбил вызов целиком («expected object, received
# string»), запись зависла в очереди хуков и осталась незаписанной — а это
# отказ на стороне клиента, сервер такого вызова не видит и починить его на
# сервере нечем. Значит тип в схеме обязан допускать строку, а сервер —
# понимать её как одно поле, а не терять вместе с вызовом.

def test_facts_schema_accepts_a_plain_string():
    tool = next(t for t in asyncio.run(tools.list_tools()) if t.name == "save_tracking")
    spec = ((tool.inputSchema or {}).get("properties") or {})["facts"]
    kinds = spec.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    assert "string" in kinds and "object" in kinds, spec


def test_prose_facts_are_stored_not_dropped(knowledge_dir):
    from memory_compiler.handlers import save_tracking, read_article
    report = "Проверка 19.09: used 2302.58, remaining 7010.4, дней осталось 12."
    out = asyncio.run(save_tracking(project="testproj", entity="daily-check",
                                    facts=report))
    assert "❌" not in "".join(c.text for c in out)
    text = "".join(c.text for c in asyncio.run(
        read_article(project="testproj", filename="tracking_daily-check.md")))
    assert "2302.58" in text, "прозаический отчёт обязан доехать до статьи: %r" % text[:300]


def test_destructive_tools_never_get_a_guessed_project(bridge):
    """Удаление по угаданному проекту недопустимо: цена ошибки — стёртая статья,
    а выигрыш — всего один сэкономленный отказ. У них project остаётся
    обязательным в схеме, и вызов без него отбивает клиент."""
    assert not (tools._PROJECT_FROM_SESSION & tools._DESTRUCTIVE), (
        "деструктивный инструмент попал в список подстановки")
    _call("search", {"query": "роутер", "project": "infra"})
    bridge.clear()          # иначе «хендлер не звали» не отличить от прошлого вызова
    _call("delete_article", {"filename": "статья.md"})
    assert bridge.get("project") != "infra", (
        "delete_article не должен получать подставленный проект: %r" % bridge)


def test_no_guess_without_a_chat_id(bridge):
    """У моста Claude Desktop одна MCP-сессия на ВСЕ чаты Code (v1.76.0). Без id
    чата ключ снимка общий, и подставленный «последний проект» оказался бы
    проектом чужого чата — запись уехала бы к соседу. Молча. Лучше отказ."""
    asyncio.run(tools.call_tool("search", {"query": "роутер", "project": "infra"}))
    bridge.clear()
    out = asyncio.run(tools.call_tool("finish_task", {"topic": "итог", "content": "текст"}))
    text = "".join(c.text for c in out if isinstance(c, TextContent))
    assert "project" in text and not bridge, (
        "без id чата проект угадывать нельзя: %r / %r" % (text, bridge))


# ── схема обязана ПУСТИТЬ вызов без project (v1.90.0) ──────────────────────
# ⚠️ Серверной подстановки МАЛО, и это проверено живьём 20.09.2026 на проде
# 1.90.0: `session_note` без project вернул `-32602 … path: ["project"]` — вызов
# отбил клиент, до сервера он не дошёл, подставлять было нечего. Пока параметр
# стоит в `required`, клиентский валидатор отбивает вызов раньше сервера, и вся
# починка остаётся мёртвой. Поэтому у инструментов, которым проект можно взять
# из истории сессии, он из `required` снят.

def test_writing_tools_do_not_require_project_in_schema():
    by_name = {t.name: t for t in asyncio.run(tools.list_tools())}
    offenders = [name for name in sorted(tools._PROJECT_FROM_SESSION)
                 if "project" in ((by_name[name].inputSchema or {}).get("required") or [])]
    assert offenders == [], (
        "пока project в required, клиент отобьёт вызов до сервера: %s" % offenders)


def test_project_stays_required_where_it_cannot_be_guessed():
    """Позитивный контроль: снимаем требование не со всех подряд. У деструктивных
    проект обязан остаться обязательным — угадывать его там нельзя."""
    by_name = {t.name: t for t in asyncio.run(tools.list_tools())}
    required = (by_name["delete_article"].inputSchema or {}).get("required") or []
    assert "project" in required, "у delete_article проект обязан остаться обязательным"
    # remove_project адресует проект параметром `name`, а не `project`, — для него
    # важно лишь то, что подстановка его не касается.
    assert "remove_project" not in tools._PROJECT_FROM_SESSION


def test_project_is_still_declared_as_a_parameter():
    """Снятие из required не должно уносить сам параметр: явный проект остаётся
    главным способом вызова, а подстановка — страховкой."""
    by_name = {t.name: t for t in asyncio.run(tools.list_tools())}
    for name in sorted(tools._PROJECT_FROM_SESSION):
        props = (by_name[name].inputSchema or {}).get("properties") or {}
        assert "project" in props, name
        assert (props["project"].get("description") or "").strip(), name
