"""Поля релиза трекера ходят вместе со своей версией (v1.96.0).

Решение владельца 25.09.2026 («А + возврат»,
memory-compiler/decision_поля_релиза_трекера_ходят_вместе_с_версией.md): при смене version
поля релиза (_VERSION_BOUND_KEYS) без новых значений уходят в history (правило v1.93.0), а
когда version возвращается к бывшей — непереданные поля релиза возвращаются из ПОСЛЕДНЕГО
снимка истории с этой версией.

auto_update_tracking передаёт только изменённые поля. Раньше он отдавал копию снимка, и
правило ухода на авто-пути не срабатывало никогда: замер 25.09.2026 по 64 живым трекерам —
30 авто-подъёмов version у трекеров с полями релиза, поля переехали к новой версии в 30 из 30.
"""
import asyncio

from memory_compiler.storage import (
    _restore_release_fields, _version_changed, _write_frontmatter, auto_update_tracking,
    load_tracking, save_tracking_article,
)

REL = {"version": "1.0.0", "commit": "abc1234", "tag": "v1.0.0", "container": "app-mcp"}

HIST = [
    {"version": "1.0.0", "commit": "aaa1111", "tag": "v1.0.0", "container": "old",
     "from": "2026-09-01", "to": "2026-09-02"},
    {"version": "1.1.0", "from": "2026-09-02", "to": "2026-09-03"},
]


def _cur(entity="release", project="testproj"):
    """current трекера без служебного since."""
    cur = load_tracking(project, entity)["current"]
    return {k: v for k, v in cur.items() if k != "since"}


# ─── Условие смены версии — одно на уход и возврат ───────────────────────


def test_version_changed_rules():
    assert _version_changed({"version": "1.0.0"}, {"version": "1.1.0"})
    assert _version_changed({"version": "1.0.0"}, {"version": None})
    assert not _version_changed({"version": "1.0.0"}, {"version": "1.0.0"})
    # YAML читает «version: 1.0» как float, клиент шлёт «1.0» строкой — не смена
    assert not _version_changed({"version": 1.0}, {"version": "1.0"})
    # трекеру без version не к чему привязать поля релиза
    assert not _version_changed({}, {"version": "1.0.0"})
    assert not _version_changed({"version": "1.0.0"}, {"port": 8765})


# ─── _restore_release_fields — чистая функция ────────────────────────────


def test_restore_takes_release_fields_of_that_version():
    """Возвращаются только поля релиза: container и служебные from/to — нет."""
    merged, restored = _restore_release_fields(
        {"version": "1.0.0", "container": "new"}, {"version": "1.0.0"}, HIST)
    assert restored == ["commit", "tag"]
    assert merged == {"version": "1.0.0", "container": "new",
                      "commit": "aaa1111", "tag": "v1.0.0"}


def test_restore_uses_latest_snapshot_of_the_version():
    hist = [{"version": "1.0.0", "commit": "old0000"}, {"version": "1.1.0"},
            {"version": "1.0.0", "commit": "new1111"}, {"version": "1.1.0"}]
    merged, restored = _restore_release_fields({"version": "1.0.0"}, {"version": "1.0.0"}, hist)
    assert restored == ["commit"] and merged["commit"] == "new1111"


def test_bare_latest_snapshot_restores_nothing():
    """Последний снимок версии «голый» — возвращать нечего, старые снимки не смотрим."""
    hist = [{"version": "1.0.0", "commit": "old0000"}, {"version": "1.1.0"},
            {"version": "1.0.0"}, {"version": "1.1.0"}]
    merged, restored = _restore_release_fields({"version": "1.0.0"}, {"version": "1.0.0"}, hist)
    assert restored == [] and "commit" not in merged


def test_passed_value_and_null_win_over_restore():
    merged, restored = _restore_release_fields(
        {"version": "1.0.0", "commit": "fresh99"},
        {"version": "1.0.0", "commit": "fresh99", "tag": None}, HIST)
    assert restored == []
    assert merged["commit"] == "fresh99" and "tag" not in merged


def test_restore_name_check_ignores_case():
    """В истории «Commit», передан «commit» — второго поля того же смысла не будет."""
    merged, restored = _restore_release_fields(
        {"version": "1.0.0", "commit": "fresh99"}, {"version": "1.0.0", "commit": "fresh99"},
        [{"version": "1.0.0", "Commit": "aaa1111"}])
    assert restored == [] and "Commit" not in merged


def test_none_in_history_is_not_restored():
    merged, restored = _restore_release_fields(
        {"version": "1.0.0"}, {"version": "1.0.0"},
        [{"version": "1.0.0", "commit": None, "tag": "v1.0.0"}])
    assert restored == ["tag"] and "commit" not in merged


def test_float_version_in_history_matches_string():
    merged, restored = _restore_release_fields(
        {"version": "1.0"}, {"version": "1.0"}, [{"version": 1.0, "commit": "aaa1111"}])
    assert restored == ["commit"]


def test_no_target_version_restores_nothing():
    merged, restored = _restore_release_fields({"container": "x"}, {"version": None}, HIST)
    assert (merged, restored) == ({"container": "x"}, [])


def test_restore_does_not_mutate_inputs():
    merged, new_facts = {"version": "1.0.0"}, {"version": "1.0.0"}
    _restore_release_fields(merged, new_facts, HIST)
    assert merged == {"version": "1.0.0"} and new_facts == {"version": "1.0.0"}


# ─── save_tracking_article: возврат при смене версии ─────────────────────


def test_rollback_after_bare_bump_restores_release_fields(knowledge_dir):
    """Главный сценарий: подъём без полей релиза снимает commit/tag, откат {version}
    возвращает их из истории, остальное снимка целое."""
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release", {"version": "1.1.0"})
    r = save_tracking_article("testproj", "release", {"version": "1.0.0"})
    assert r["action"] == "updated"
    assert r["restored_with_version"] == ["commit", "tag"]
    assert _cur() == REL


def test_rollback_over_chain_restores_fields_of_that_version(knowledge_dir):
    """Живой случай hzti_ut/deployment 12.09.2026: 0.3.41 → дата → дата → откат к 0.3.41."""
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release", {"version": "1.1.0"})
    save_tracking_article("testproj", "release", {"version": "1.2.0"})
    r = save_tracking_article("testproj", "release", {"version": "1.0.0"})
    assert r["restored_with_version"] == ["commit", "tag"]
    assert _cur() == REL


def test_no_restore_without_version_change(knowledge_dir):
    """commit, снятый null при той же версии, запись другого поля не возвращает."""
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release", {"commit": None})
    r = save_tracking_article("testproj", "release", {"container": "app-mcp-2"})
    assert r["restored_with_version"] == []
    assert "commit" not in _cur()


def test_replace_does_not_restore(knowledge_dir):
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release", {"version": "1.1.0"})
    r = save_tracking_article("testproj", "release", {"version": "1.0.0"}, replace=True)
    assert r["restored_with_version"] == []
    assert _cur() == {"version": "1.0.0"}


def test_version_null_does_not_restore(knowledge_dir):
    save_tracking_article("testproj", "release", REL)
    r = save_tracking_article("testproj", "release", {"version": None})
    assert r["restored_with_version"] == []
    assert _cur() == {"container": "app-mcp"}


def test_bump_to_version_from_history_restores_its_fields(knowledge_dir):
    """X+1 с commit/tag → ошибочный откат к X → подъём к X+1 возвращает поля X+1."""
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release",
                          {"version": "1.1.0", "commit": "bbb2222", "tag": "v1.1.0"})
    save_tracking_article("testproj", "release", {"version": "1.0.0"})
    r = save_tracking_article("testproj", "release", {"version": "1.1.0"})
    assert r["restored_with_version"] == ["commit", "tag"]
    assert (_cur()["commit"], _cur()["tag"]) == ("bbb2222", "v1.1.0")


def test_guard_held_version_restores_nothing(knowledge_dir):
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release", {"version": "1.1.0"})
    r = save_tracking_article("testproj", "release", {"version": "1.0.0"},
                              guard_version_regression=True)
    assert r["action"] == "unchanged" and r["restored_with_version"] == []
    assert "commit" not in _cur()


# ─── Авто-путь: только изменённые поля ───────────────────────────────────

DEP = {"version": "1.0.0", "commit": "abc1234", "tag": "v1.0.0",
       "container": "app-mcp", "port": 8765}


def test_auto_bump_moves_release_fields_to_history(knowledge_dir):
    """Авто-подъём снимает commit/tag прежней версии, прочие поля целы, в истории —
    прежний снимок целиком. До v1.96.0 поля переезжали к новой версии: полный снимок."""
    save_tracking_article("testproj", "deployment", DEP)
    updates = auto_update_tracking("testproj", "deployment: выкатили 1.1.0", "Deploy update")
    assert len(updates) == 1
    assert _cur("deployment") == {"version": "1.1.0", "container": "app-mcp", "port": 8765}
    last = load_tracking("testproj", "deployment")["history"][-1]
    assert (last["version"], last["commit"], last["tag"]) == ("1.0.0", "abc1234", "v1.0.0")


def test_auto_ip_change_keeps_release_fields(knowledge_dir):
    save_tracking_article("testproj", "gw", {"version": "1.0.0", "commit": "abc1234",
                                             "ip": "10.0.0.1"})
    updates = auto_update_tracking("testproj", "gw: новый адрес 10.0.0.2", "gw")
    assert len(updates) == 1
    assert _cur("gw") == {"version": "1.0.0", "commit": "abc1234", "ip": "10.0.0.2"}


def test_auto_update_keeps_null_field(knowledge_dir):
    """Дефект v1.93.0: полный снимок нёс None, а слияние читает None как «удалить поле» —
    поле со значением null исчезало при любом авто-апдейте. Трекер пишется напрямую:
    через save_tracking_article null-поле не записать (null удаляет поле)."""
    data = {"type": "tracking", "project": "testproj", "entity": "site",
            "current": {"url": "https://old.example.com", "vk": None, "status": "ok"},
            "history": []}
    (knowledge_dir / "testproj" / "tracking_site.md").write_text(
        _write_frontmatter(data) + "\n# Site\n", encoding="utf-8")
    updates = auto_update_tracking("testproj", "site: новый адрес https://new.example.com", "site")
    assert len(updates) == 1
    cur = _cur("site")
    assert "new.example.com" in cur["url"]
    assert "vk" in cur and cur["vk"] is None


def test_auto_guard_held_version_changes_nothing(knowledge_dir):
    save_tracking_article("testproj", "deployment",
                          {**DEP, "version": "1.2.0", "commit": "ccc3333", "tag": "v1.2.0"})
    updates = auto_update_tracking("testproj", "deployment: выкатили 1.1.0", "Deploy update")
    assert updates == []
    assert _cur("deployment")["commit"] == "ccc3333"


def test_auto_update_journal_names_dropped_fields(knowledge_dir):
    save_tracking_article("testproj", "deployment", DEP)
    auto_update_tracking("testproj", "deployment: выкатили 1.1.0", "Deploy update")
    log = (knowledge_dir / "testproj" / "_log.md").read_text(encoding="utf-8")
    assert "deployment: version: 1.0.0→1.1.0, commit: abc1234→—, tag: v1.0.0→—" in log, log


def test_false_auto_bump_then_rollback_restores_fields(knowledge_dir):
    """Главный сценарий решения владельца: авто-путь ложно поднял версию и снял
    commit/tag, явный откат {version} их возвращает — откат ложного подъёма остаётся
    без потерь, как до v1.96.0."""
    save_tracking_article("testproj", "deployment", DEP)
    auto_update_tracking("testproj", "deployment: выкатили 1.1.0", "Deploy update")
    r = save_tracking_article("testproj", "deployment", {"version": "1.0.0"})
    assert r["restored_with_version"] == ["commit", "tag"]
    assert _cur("deployment") == DEP


def test_lesson_summary_shows_dropped_fields_and_hint(knowledge_dir):
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "deployment", DEP)
    out = asyncio.run(save_lesson("Deploy update", "deployment: выкатили 1.1.0",
                                  "testproj", ["deploy"]))
    text = out[0].text
    assert ("🔄 tracking/deployment: version: 1.0.0 → 1.1.0, commit: abc1234 → —, "
            "tag: v1.0.0 → —; для 1.1.0 передай их через save_tracking, если знаешь") in text, text


def test_lesson_summary_without_dropped_fields_has_no_hint(knowledge_dir):
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "gw", {"version": "1.0.0", "commit": "abc1234",
                                             "ip": "10.0.0.1"})
    out = asyncio.run(save_lesson("gw", "gw: новый адрес 10.0.0.2", "testproj", ["infra"]))
    text = out[0].text
    assert "🔄 tracking/gw: ip: 10.0.0.1 → 10.0.0.2" in text, text
    assert "передай их" not in text, text


# ─── Ответ save_tracking и описание facts ────────────────────────────────


def test_response_names_restored_fields(knowledge_dir):
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release", {"version": "1.1.0"})
    out = asyncio.run(save_tracking(project="testproj", entity="release",
                                    facts={"version": "1.0.0"}))
    text = out[0].text
    assert "\n  вернулись из истории вместе с версией 1.0.0: commit, tag" in text, text
    assert "ушли в историю" not in text, text


def test_response_has_no_hint_when_everything_came_back(knowledge_dir):
    """X+1 с commit/tag → откат к X: commit/tag X+1 ушли, commit/tag X вернулись —
    подсказка «передай их заново» соврала бы."""
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release",
                          {"version": "1.1.0", "commit": "bbb2222", "tag": "v1.1.0"})
    out = asyncio.run(save_tracking(project="testproj", entity="release",
                                    facts={"version": "1.0.0"}))
    text = out[0].text
    assert "ушли в историю вместе с версией 1.1.0: commit, tag" in text, text
    assert "вернулись из истории вместе с версией 1.0.0: commit, tag" in text, text
    assert "передай их заново" not in text, text


def test_response_keeps_hint_when_something_did_not_come_back(knowledge_dir):
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "release", REL)
    save_tracking_article("testproj", "release",
                          {"version": "1.1.0", "commit": "bbb2222", "tag": "v1.1.0",
                           "tests": "12 passed"})
    out = asyncio.run(save_tracking(project="testproj", entity="release",
                                    facts={"version": "1.0.0"}))
    text = out[0].text
    assert ("ушли в историю вместе с версией 1.1.0: commit, tag, tests — для 1.0.0 "
            "передай их заново, если знаешь") in text, text
    assert "вернулись из истории вместе с версией 1.0.0: commit, tag" in text, text


def test_facts_description_names_return():
    from memory_compiler.tools import list_tools
    tool = {t.name: t for t in asyncio.run(list_tools())}["save_tracking"]
    assert "возвращаются из её снимка" in tool.inputSchema["properties"]["facts"]["description"]
