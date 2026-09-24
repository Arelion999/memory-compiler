"""Контекст без спроса при первом обращении к проекту (v1.68.0).

Замер 26.08.2026 по аудиту (109 сессий за месяц, нарезка по разрыву 60 минут):
**81% сессий начинаются не с загрузки контекста** — первым вызовом идут
`save_lesson`, `edit_article`, `search`, `finish_task`. Из этих «слепых» сессий
85 ПИШУТ в базу, то есть работают всерьёз, не прочитав, на чём остановились.
У 86 из 89 по проекту было что показать (открытые вопросы или незакрытая
сессия), а частота подсказки вышла бы около 3 раз в сутки — это не шум.

Механизм тот же, что у футера свежести и напоминания о заметке: сервер
дописывает подсказку сам, отдельным блоком, ровно один раз на пару
(сессия, проект).
"""

import asyncio

import pytest
from mcp.types import TextContent

from memory_compiler import freshness, handlers, storage


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(handlers, "KNOWLEDGE_DIR", tmp_path)
    import memory_compiler.config as cfg
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    (tmp_path / "demo").mkdir()
    return "demo"


@pytest.fixture(autouse=True)
def clean():
    freshness.reset()
    yield
    freshness.reset()


def test_hint_carries_open_questions(proj):
    storage.add_question(proj, "Не проверено, доезжает ли конфиг до прода")
    hint = handlers.first_touch_context(proj)
    assert "доезжает ли конфиг" in hint
    assert "start_task" in hint, "должно быть сказано, где взять полный контекст"


def test_hint_carries_unfinished_session(proj):
    storage.append_note(proj, "прод отдаёт 502 после рестарта")
    hint = handlers.first_touch_context(proj)
    assert "502 после рестарта" in hint


def test_silence_when_there_is_nothing_to_say(proj):
    """Пустая подсказка — это шум. Нет вопросов и незакрытой сессии — молчим."""
    assert handlers.first_touch_context(proj) == ""


def test_finished_session_alone_is_not_a_reason(proj):
    """Закрытая вчерашняя сессия — не повод дёргать: это обычная история проекта,
    за ней идут в start_task. Поводом служат только открытые вопросы и
    незакрытая работа."""
    storage.append_session(proj, "вчера всё доделали")
    assert handlers.first_touch_context(proj) == ""


def test_hint_is_compact(proj):
    storage.add_question(proj, "Очень длинный вопрос. " * 60)
    storage.append_note(proj, "И длинная заметка по ходу. " * 60)
    hint = handlers.first_touch_context(proj)
    assert len(hint) <= handlers.FIRST_TOUCH_CHARS, f"подсказка разрослась до {len(hint)}"


def test_only_on_first_touch_of_a_project():
    """Второй раз по тому же проекту молчим — иначе это уже не подсказка, а фон."""
    key = freshness.key_for(object())
    assert freshness.is_first_touch(key, "demo") is True
    freshness.touch(key, "demo")
    assert freshness.is_first_touch(key, "demo") is False


def test_different_projects_each_get_one_hint():
    key = freshness.key_for(object())
    assert freshness.is_first_touch(key, "infra") is True
    freshness.touch(key, "infra")
    assert freshness.is_first_touch(key, "niksdesk") is True


def test_context_loading_tools_are_excluded():
    """Если сессия и так зовёт start_task, подсказка дублировала бы его выдачу."""
    from memory_compiler import tools
    for name in ("start_task", "load_session", "get_active_context", "open_questions"):
        assert name in tools._CONTEXT_TOOLS
    assert "save_lesson" not in tools._CONTEXT_TOOLS


def test_consolidate_is_not_treated_as_a_lookup():
    """consolidate ПИШЕТ (он в _FRESHNESS_WRITE_TOOLS), а _LOOKUP_TOOLS считался
    как _READONLY_LOCAL - _CONTEXT_TOOLS и не вычитал пишущие — справочный вызов
    из чужого проекта по ошибке не давал подсказки первого обращения."""
    from memory_compiler import tools
    assert "consolidate" not in tools._LOOKUP_TOOLS
    assert "read_article" in tools._LOOKUP_TOOLS


@pytest.mark.asyncio
async def test_notice_reaches_the_model_for_tools_with_output_schema(proj, monkeypatch):
    """У `search` объявлен outputSchema — клиент берёт structuredContent, а
    дополнительный TextContent модели не показывает. Проверено на проде: сервер
    отдавал подсказку вторым текстовым блоком, и она никуда не доходила. А
    именно `search` чаще всего и открывает «слепую» сессию (17 из 89 по замеру).
    Поэтому футеры дублируются полем `notice` структурированной выдачи.
    """
    from memory_compiler import tools
    payload = {"query": "тест", "count": 0, "results": []}
    out = tools._merge_notice_into_payload(payload, "\n\n📌 подсказка про проект")
    assert "подсказка про проект" in out["notice"]

    schema = None
    for t in await tools.list_tools():
        if t.name == "search":
            schema = t.outputSchema
    assert "notice" in schema["properties"], "поле обязано быть в схеме, иначе клиент его отбросит"
    assert "notice" not in schema["required"], "подсказка есть не всегда"


def test_notice_is_absent_when_there_is_nothing_to_say():
    from memory_compiler import tools
    payload = {"query": "тест", "count": 0, "results": []}
    assert "notice" not in tools._merge_notice_into_payload(payload, "")


# ── справка из чужого проекта (v1.91.0) ─────────────────────────────────────
# Из 66 подсказок «Первое обращение» за неделю 43 пришлись на read_article, чаще
# всего по проекту, который сессия читала для справки: ~500 символов чужих
# открытых вопросов в ответ на чтение одной статьи.

class _FakeSession:
    """Заглушка MCP-сессии: важна только идентичность."""


@pytest.fixture
def bridge(monkeypatch):
    from memory_compiler import tools

    class Ctx:
        session = _FakeSession()

    class FakeApp:
        request_context = Ctx()

    async def fake_dispatch(name, arguments):
        return [TextContent(type="text", text="ok")]

    monkeypatch.setattr(tools, "app", FakeApp())
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_dispatch_tool", fake_dispatch)
    monkeypatch.setattr(handlers, "first_touch_context",
                        lambda project: f"\n\n📌 подсказка {project}")
    return tools


def _call(tools, name, args, chat="chat-a"):
    out = asyncio.run(tools.call_tool(name, {**args, freshness.CLIENT_SESSION_ARG: chat}))
    return "".join(c.text for c in out if isinstance(c, TextContent))


def test_lookup_in_a_foreign_project_gets_no_hint(bridge):
    _call(bridge, "save_lesson", {"topic": "t", "content": "c", "project": "infra"})
    out = _call(bridge, "read_article", {"project": "general", "filename": "справка.md"})
    assert "📌" not in out, "справка из чужого проекта получила подсказку первого обращения"


def test_first_read_in_a_fresh_chat_still_gets_the_hint(bridge):
    """Позитивный контроль: слепая сессия, начавшая с чтения, подсказку получает."""
    out = _call(bridge, "read_article", {"project": "general", "filename": "справка.md"},
                chat="chat-new")
    assert "📌 подсказка general" in out


def test_first_write_into_another_project_still_gets_the_hint(bridge):
    """Правило только для справки: запись в новый проект — повод показать его
    открытые вопросы, даже если сессия работала с другим."""
    _call(bridge, "save_lesson", {"topic": "t", "content": "c", "project": "infra"})
    out = _call(bridge, "save_lesson", {"topic": "t2", "content": "c2", "project": "general"})
    assert "📌 подсказка general" in out
