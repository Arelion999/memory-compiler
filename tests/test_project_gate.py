"""Чтение не заводит проект, а проекта, которого нет, хендлер не видит.

Живой случай 14.09.2026 16:19:16 (+10): агент угадал project="memorycompiler" по
каталогу клона D:\\MCP\\MemoryCompiler и вызвал start_task. storage.project_dir
делал mkdir под любое имя, в том числе на чтении, и на сервере появился пустой
каталог knowledge/memorycompiler — время создания совпало с вызовом в
app.jsonl до миллисекунд. Настоящий проект — memory-compiler (37 статей), а
пустой двойник потом сбивал route_project, подтверждая угаданное имя.

Отсюда два уровня защиты:
- storage: путь (project_path) и создание (project_dir) разделены, каталог
  создаёт только запись;
- tools.call_tool: чтение несуществующего проекта получает подсказку вместо
  пустого ответа, а запись в двойника по ключу (регистр, '-', '_') — отказ.
  Молча подставить двойника нельзя: проект назван явно, а переписать явный
  выбор хуже, чем отказать.
"""

import asyncio

import pytest
from mcp.types import TextContent

from memory_compiler import freshness, storage, tools


def _text(blocks) -> str:
    return "".join(getattr(b, "text", "") or "" for b in blocks)


def _dirs(kd) -> list[str]:
    """Каталоги проектов; служебные скрытые (.whoosh_index) — не проекты."""
    return sorted(p.name for p in kd.iterdir() if p.is_dir() and not p.name.startswith("."))


@pytest.fixture
def base(knowledge_dir):
    """Настоящий проект со статьёй — как memory-compiler на сервере."""
    proj = knowledge_dir / "memory-compiler"
    proj.mkdir()
    (proj / "deploy.md").write_text("# Деплой\n\n**Теги:** deploy\n\nтело\n",
                                    encoding="utf-8")
    return knowledge_dir


# ── storage: путь и создание каталога разделены ───────────────────────────


def test_project_path_does_not_create(knowledge_dir):
    before = _dirs(knowledge_dir)
    path = storage.project_path("MemoryCompiler")
    assert path == knowledge_dir / "memorycompiler"
    assert _dirs(knowledge_dir) == before


def test_safe_paths_do_not_create(knowledge_dir):
    before = _dirs(knowledge_dir)
    storage.safe_project_path("ghost")
    storage.safe_article_path("ghost", "note.md")
    assert _dirs(knowledge_dir) == before


def test_write_path_still_creates(knowledge_dir):
    """Позитивный контроль: запись заводит проект, как и раньше."""
    assert storage.safe_project_dir("fresh").is_dir()
    assert storage.project_dir("fresh2").is_dir()


def test_unsafe_name_leaves_no_directory(knowledge_dir):
    """Проверка имени идёт ДО mkdir: отвергнутое имя не оставляет каталога."""
    before = _dirs(knowledge_dir)
    for bad in ("all", "a/b", ".."):
        with pytest.raises(ValueError):
            storage.safe_project_dir(bad)
    assert _dirs(knowledge_dir) == before


def test_twins_by_key(base):
    (base / "memory_compiler").mkdir()
    assert storage.project_key("Memory_Compiler") == storage.project_key("memory-compiler")
    assert storage.project_key(".claude") != storage.project_key("claude")
    # больше статей — первым; сам проект в список не входит
    assert storage.project_twins("memorycompiler") == [
        ("memory-compiler", 1), ("memory_compiler", 0)]
    assert storage.project_twins("memory-compiler") == [("memory_compiler", 0)]
    assert storage.project_twins("testproj") == []


def test_near_names_exclude_twins(base):
    assert storage.project_near_names("memory-compile") == ["memory-compiler"]
    assert storage.project_near_names("memorycompiler") == []


# ── чтение несуществующего проекта не оставляет каталога ───────────────────
# Хендлеры зовутся НАПРЯМУЮ, мимо call_tool: гарантия уровня storage обязана
# держаться и для REST API, и для будущих инструментов без подсказки.

_READERS = [
    ("start_task", {"topic": "продолжим"}),       # continuation: без эмбеддингов
    ("load_session", {}),
    ("get_active_context", {}),
    ("get_context", {}),
    ("get_summary", {}),
    ("open_questions", {}),
    ("get_project_deps", {}),
    ("get_current", {"entity": "release"}),
    ("read_article", {"filename": "a.md"}),
    ("backlinks", {"filename": "a.md"}),
    ("article_history", {"filename": "a.md"}),
    ("get_runbook", {"filename": "a.md"}),
    ("close_question", {"match": "что-то"}),
    ("lint", {"fix": True}),
    ("gap_report", {}),
    ("stale_facts", {}),
]


@pytest.mark.parametrize("tool,args", _READERS, ids=[t for t, _ in _READERS])
def test_reading_handler_does_not_create_project(knowledge_dir, tool, args):
    from memory_compiler import handlers
    before = _dirs(knowledge_dir)
    asyncio.run(getattr(handlers, tool)(project="memorycompiler", **args))
    assert _dirs(knowledge_dir) == before, f"{tool} завёл каталог проекта"


def test_first_touch_context_does_not_create_project(knowledge_dir):
    from memory_compiler import handlers
    before = _dirs(knowledge_dir)
    handlers.first_touch_context("memorycompiler")
    assert _dirs(knowledge_dir) == before


# ── call_tool: подсказка на чтении, отказ на записи в двойника ─────────────


@pytest.fixture
def chat(monkeypatch):
    """MCP-сессия с id чата — чтобы работали сторож свежести и подстановка проекта."""
    from memory_compiler import handlers

    class Ctx:
        session = object()

    class FakeApp:
        request_context = Ctx()

    monkeypatch.setattr(tools, "app", FakeApp())
    monkeypatch.setattr(handlers, "first_touch_context", lambda project: "")
    monkeypatch.setattr(tools, "audit_log", lambda *a, **k: None)
    freshness.reset()
    yield


def _call(name, args):
    return asyncio.run(tools.call_tool(
        name, {**args, freshness.CLIENT_SESSION_ARG: "chat-a"}))


def test_read_of_twin_names_the_real_project(base, chat):
    """Регресс 14.09.2026: start_task по угаданному имени."""
    before = _dirs(base)
    out = _text(_call("start_task", {"topic": "продолжим", "project": "memorycompiler"}))
    assert "memorycompiler" in out and "«memory-compiler»" in out, out
    assert 'project="memory-compiler"' in out, "подсказка обязана дать готовый вызов"
    assert _dirs(base) == before


def test_read_of_unknown_project_without_twin_points_to_list(base, chat):
    out = _text(_call("get_active_context", {"project": "nosuch"}))
    assert "nosuch" in out and "list_projects" in out, out
    assert "nosuch" not in _dirs(base)


def test_read_hint_names_close_typo(base, chat):
    out = _text(_call("load_session", {"project": "memory-compile"}))
    assert "«memory-compiler»" in out, out


def test_search_hint_reaches_structured_output(base, chat):
    """У search модель видит ТОЛЬКО structuredContent — подсказка обязана быть в notice."""
    res = asyncio.run(tools.call_tool("search", {"query": "деплой", "project": "memorycompiler"}))
    blocks, structured = res
    assert structured["count"] == 0
    assert "memory-compiler" in structured.get("notice", ""), structured


def test_refused_read_is_not_remembered_as_session_project(base, chat):
    """Сторож свежести при отказе не зовётся: иначе несуществующий проект стал бы
    «последним проектом сессии», и запись без project ушла бы в него — ровно тот
    двойник, от которого защищаемся."""
    _call("start_task", {"topic": "продолжим", "project": "memorycompiler"})
    out = _text(_call("session_note", {"note": "факт"}))
    assert "подставить его не из чего" in out, out
    assert "memorycompiler" not in _dirs(base)


def test_write_to_twin_is_refused_not_redirected(base, chat):
    before = _dirs(base)
    out = _text(_call("session_note", {"note": "факт", "project": "memorycompiler"}))
    assert out.startswith("❌ Ничего не записано"), out
    assert 'project="memory-compiler"' in out and "add_project" in out, out
    assert _dirs(base) == before
    assert not (base / "memory-compiler" / "_session.md").exists(), "подменять проект нельзя"


def test_write_to_new_project_creates_it_and_says_so(base, chat):
    out = _text(_call("session_note", {"note": "факт", "project": "brand-new"}))
    assert (base / "brand-new" / "_session.md").exists()
    assert "Заведён новый проект «brand-new»" in out, out


def test_write_with_typo_is_kept_but_named(base, chat):
    """Опечатка — не двойник по ключу: похожими бывают и честно разные проекты
    (client-a и client-b), поэтому запись проходит, но похожий проект назван."""
    out = _text(_call("session_note", {"note": "факт", "project": "memory-compile"}))
    assert (base / "memory-compile").is_dir()
    assert "«memory-compiler»" in out, out


def test_explicit_add_project_unblocks_the_twin(base, chat):
    """Отдельный проект с похожим именем заводится явно — и дальше запись идёт."""
    _call("add_project", {"name": "memorycompiler"})
    out = _text(_call("session_note", {"note": "факт", "project": "memorycompiler"}))
    assert (base / "memorycompiler" / "_session.md").exists(), out


def test_existing_empty_twin_is_flagged(base, chat):
    """Двойник, заведённый старым mkdir на чтении, доживает до уборки — ответы по нему
    называют проект со статьями."""
    twin = base / "memorycompiler"
    twin.mkdir()
    (twin / "_session.md").write_text("# Сессии: memorycompiler\n", encoding="utf-8")
    out = _text(_call("get_active_context", {"project": "memorycompiler"}))
    assert "нет ни одной статьи" in out and "«memory-compiler»" in out, out


def test_ordinary_answers_stay_byte_for_byte(base, chat):
    """Позитивный контроль: у существующего непустого проекта сносок нет."""
    out = _call("get_active_context", {"project": "memory-compiler"})
    assert _text(out) == "Нет активного контекста для memory-compiler.", _text(out)


def test_unsafe_name_keeps_handler_error(base, chat):
    out = _text(_call("load_session", {"project": "../etc"}))
    assert "Небезопасный" in out, out
