"""Слияние фактов в save_tracking (v1.93.0).

Решение владельца 25.09.2026: переданные поля заменяются, непереданные остаются, null
удаляет поле, replace=True — прежняя полная замена. До v1.93.0 новый снимок собирался
как dict(new_facts): живой случай 25.09.2026 — откат ложно поднятой версии записью
{version: 1.92.1} стёр у tracking/deployment поля container, port и deploy.

Поля релиза (commit, tag, tests, verified и варианты, _VERSION_BOUND_KEYS) при смене
version без новых значений уходят в history вместе со старой версией: иначе снимок
врал бы — tag v1.92.2 рядом с version 1.93.0.
"""
import asyncio

from memory_compiler.storage import _merge_tracking_facts, load_tracking, save_tracking_article

DEPLOY = {"version": "1.92.2", "container": "memory-compiler-mcp", "port": 8765,
          "deploy": "SynologyDrive volume + mc-watcher"}


def _history_len(entity, project="testproj"):
    # Пустой history пишется как «history:» и читается как None — отсюда «or []».
    return len(load_tracking(project, entity).get("history") or [])


def test_merge_keeps_fields_that_were_not_passed(knowledge_dir):
    """Живой случай 25.09.2026: откат версии одной записью {version} больше не стирает
    container, port и deploy, а прежний снимок целиком лежит в истории."""
    save_tracking_article("testproj", "deployment", DEPLOY)
    r = save_tracking_article("testproj", "deployment", {"version": "1.92.1"})
    assert r["action"] == "updated"
    assert r["dropped_with_version"] == []
    data = load_tracking("testproj", "deployment")
    cur = data["current"]
    assert cur["version"] == "1.92.1"
    assert cur["container"] == "memory-compiler-mcp"
    assert cur["port"] == 8765
    assert cur["deploy"] == "SynologyDrive volume + mc-watcher"
    last = data["history"][-1]
    assert {k: last[k] for k in DEPLOY} == DEPLOY


def test_passed_field_replaced_others_intact(knowledge_dir):
    save_tracking_article("testproj", "deployment", DEPLOY)
    save_tracking_article("testproj", "deployment", {"port": 8766})
    cur = load_tracking("testproj", "deployment")["current"]
    assert cur["port"] == 8766
    assert cur["version"] == "1.92.2" and cur["container"] == "memory-compiler-mcp"


def test_null_removes_field_and_history_keeps_it(knowledge_dir):
    save_tracking_article("testproj", "deployment", {**DEPLOY, "note": "временно"})
    r = save_tracking_article("testproj", "deployment", {"note": None})
    assert r["action"] == "updated"
    data = load_tracking("testproj", "deployment")
    assert "note" not in data["current"]
    assert data["current"]["container"] == "memory-compiler-mcp"
    assert data["history"][-1]["note"] == "временно"


def test_null_for_missing_field_is_unchanged(knowledge_dir):
    save_tracking_article("testproj", "deployment", DEPLOY)
    r = save_tracking_article("testproj", "deployment", {"note": None})
    assert r["action"] == "unchanged"
    assert r["dropped_with_version"] == []
    assert _history_len("deployment") == 0


def test_null_on_new_tracker_is_not_written(knowledge_dir):
    r = save_tracking_article("testproj", "gw", {"status": "ok", "note": None})
    assert r["action"] == "created"
    cur = load_tracking("testproj", "gw")["current"]
    assert cur["status"] == "ok" and "note" not in cur


def test_replace_keeps_only_passed_fields(knowledge_dir):
    save_tracking_article("testproj", "deployment", DEPLOY)
    r = save_tracking_article("testproj", "deployment", {"version": "1.93.0"}, replace=True)
    assert r["action"] == "updated"
    cur = load_tracking("testproj", "deployment")["current"]
    assert {k: v for k, v in cur.items() if k != "since"} == {"version": "1.93.0"}


def test_replace_with_same_snapshot_is_unchanged(knowledge_dir):
    save_tracking_article("testproj", "deployment", DEPLOY)
    r = save_tracking_article("testproj", "deployment", dict(DEPLOY), replace=True)
    assert r["action"] == "unchanged"
    assert _history_len("deployment") == 0


def test_replace_with_subset_is_updated(knowledge_dir):
    save_tracking_article("testproj", "deployment", DEPLOY)
    r = save_tracking_article("testproj", "deployment",
                              {"version": "1.92.2", "container": "memory-compiler-mcp"},
                              replace=True)
    assert r["action"] == "updated"
    cur = load_tracking("testproj", "deployment")["current"]
    assert "port" not in cur and "deploy" not in cur


def test_merge_with_matching_subset_is_unchanged(knowledge_dir):
    save_tracking_article("testproj", "deployment", DEPLOY)
    r = save_tracking_article("testproj", "deployment", {"version": "1.92.2", "port": 8765})
    assert r["action"] == "unchanged"
    assert _history_len("deployment") == 0


def test_guard_holds_version_and_keeps_other_fields(knowledge_dir):
    save_tracking_article("testproj", "deployment", DEPLOY)
    r = save_tracking_article("testproj", "deployment", {"version": "1.7.14"},
                              guard_version_regression=True)
    assert r["action"] == "unchanged"
    cur = load_tracking("testproj", "deployment")["current"]
    assert cur["version"] == "1.92.2" and cur["container"] == "memory-compiler-mcp"


def test_merge_helper_skips_service_keys():
    merged, dropped = _merge_tracking_facts(
        {"version": "1.0.0", "since": "2026-09-01", "from": "x", "to": "y",
         "ip": "192.0.2.1"},
        {"ip": "192.0.2.2"}, replace=False)
    assert merged == {"version": "1.0.0", "ip": "192.0.2.2"}
    assert dropped == []


def test_string_facts_change_only_note(knowledge_dir):
    """Строковый facts ложится полем note (v1.90.0) и со слиянием остальной снимок не
    стирает."""
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "gw", {"status": "ok", "ip": "192.0.2.1"})
    asyncio.run(save_tracking(project="testproj", entity="gw",
                              facts="суточная проверка: всё в норме"))
    cur = load_tracking("testproj", "gw")["current"]
    assert cur["note"] == "суточная проверка: всё в норме"
    assert cur["status"] == "ok" and cur["ip"] == "192.0.2.1"


# ─── Поля релиза при смене version (вариант A, v1.93.0) ──────────────────

RELEASE = {"version": "1.92.2", "commit": "924222b", "tag": "v1.92.2",
           "repo": "github.com/example/app"}


def test_version_change_moves_release_fields_to_history(knowledge_dir):
    save_tracking_article("testproj", "release", RELEASE)
    r = save_tracking_article("testproj", "release", {"version": "1.93.0"})
    assert r["dropped_with_version"] == ["commit", "tag"]
    data = load_tracking("testproj", "release")
    cur = data["current"]
    assert cur["version"] == "1.93.0" and cur["repo"] == "github.com/example/app"
    assert "commit" not in cur and "tag" not in cur
    last = data["history"][-1]
    assert (last["version"], last["commit"], last["tag"]) == ("1.92.2", "924222b", "v1.92.2")


def test_passed_release_field_is_kept(knowledge_dir):
    save_tracking_article("testproj", "release", RELEASE)
    r = save_tracking_article("testproj", "release", {"version": "1.93.0", "commit": "abc1234"})
    assert r["dropped_with_version"] == ["tag"]
    cur = load_tracking("testproj", "release")["current"]
    assert cur["commit"] == "abc1234" and "tag" not in cur


def test_same_version_keeps_release_fields(knowledge_dir):
    save_tracking_article("testproj", "release", RELEASE)
    r = save_tracking_article("testproj", "release", {"version": "1.92.2", "status": "pushed"})
    assert r["action"] == "updated" and r["dropped_with_version"] == []
    cur = load_tracking("testproj", "release")["current"]
    assert cur["commit"] == "924222b" and cur["tag"] == "v1.92.2"


def test_tracker_without_version_keeps_release_fields(knowledge_dir):
    save_tracking_article("testproj", "build", {"commit": "924222b", "status": "ok"})
    r = save_tracking_article("testproj", "build", {"version": "1.0.0"})
    assert r["dropped_with_version"] == []
    assert load_tracking("testproj", "build")["current"]["commit"] == "924222b"


def test_release_field_names_ignore_case(knowledge_dir):
    save_tracking_article("testproj", "release", {"version": "1.92.2", "Commit": "924222b"})
    r = save_tracking_article("testproj", "release", {"version": "1.93.0"})
    assert r["dropped_with_version"] == ["Commit"]
    assert "Commit" not in load_tracking("testproj", "release")["current"]


def test_version_null_moves_release_fields_to_history(knowledge_dir):
    save_tracking_article("testproj", "release", RELEASE)
    r = save_tracking_article("testproj", "release", {"version": None})
    assert r["dropped_with_version"] == ["commit", "tag"]
    cur = load_tracking("testproj", "release")["current"]
    assert set(cur) - {"since"} == {"repo"}


def test_replace_reports_no_release_drop(knowledge_dir):
    save_tracking_article("testproj", "release", RELEASE)
    r = save_tracking_article("testproj", "release", {"version": "1.93.0"}, replace=True)
    assert r["dropped_with_version"] == []


def test_guard_held_version_drops_nothing(knowledge_dir):
    save_tracking_article("testproj", "release", RELEASE)
    r = save_tracking_article("testproj", "release", {"version": "1.92.1"},
                              guard_version_regression=True)
    assert r["action"] == "unchanged" and r["dropped_with_version"] == []
    assert load_tracking("testproj", "release")["current"]["tag"] == "v1.92.2"


def test_numeric_version_type_is_not_a_change():
    """YAML читает «version: 1.0» как float, клиент шлёт «1.0» строкой — это не смена."""
    merged, dropped = _merge_tracking_facts({"version": 1.0, "commit": "abc1234"},
                                            {"version": "1.0"}, replace=False)
    assert dropped == [] and merged["commit"] == "abc1234"


# ─── Хендлер save_tracking: replace, ответ, схема (v1.93.0) ───────────────


def test_handler_replace_needs_real_true(knowledge_dir):
    """Замену включает только настоящий True: строка «true» даёт слияние — ошибка
    уходит в безопасную сторону."""
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "deployment", DEPLOY)
    asyncio.run(save_tracking(project="testproj", entity="deployment",
                              facts={"version": "1.92.3"}, replace="true"))
    assert load_tracking("testproj", "deployment")["current"]["container"] == "memory-compiler-mcp"

    asyncio.run(save_tracking(project="testproj", entity="deployment",
                              facts={"version": "1.92.4"}, replace=True))
    cur = load_tracking("testproj", "deployment")["current"]
    assert {k: v for k, v in cur.items() if k != "since"} == {"version": "1.92.4"}


def test_response_names_removed_and_released_fields(knowledge_dir):
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "release", {**RELEASE, "note": "черновик"})
    out = asyncio.run(save_tracking(project="testproj", entity="release",
                                    facts={"version": "1.93.0", "note": None}))
    text = out[0].text
    assert "удалены: note" in text, text
    assert ("ушли в историю вместе с версией 1.92.2: commit, tag — для 1.93.0 "
            "передай их заново, если знаешь") in text, text


def test_response_after_version_null_has_no_new_version(knowledge_dir):
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "release", RELEASE)
    out = asyncio.run(save_tracking(project="testproj", entity="release",
                                    facts={"version": None}))
    text = out[0].text
    assert "удалены: version" in text, text
    assert "ушли в историю вместе с версией 1.92.2: commit, tag" in text, text
    assert "передай их заново" not in text, text


def test_response_names_removed_field_without_version_change(knowledge_dir):
    """null без смены версии: ответ называет удалённое поле, а строки про уход полей
    релиза в историю нет."""
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "deployment", {**DEPLOY, "note": "временно"})
    out = asyncio.run(save_tracking(project="testproj", entity="deployment",
                                    facts={"note": None}))
    text = out[0].text
    assert "удалены: note" in text, text
    assert "ушли в историю" not in text, text


def test_response_without_removals_has_no_extra_lines(knowledge_dir):
    from memory_compiler.handlers import save_tracking
    save_tracking_article("testproj", "deployment", DEPLOY)
    out = asyncio.run(save_tracking(project="testproj", entity="deployment",
                                    facts={"port": 8766}))
    text = out[0].text
    assert "удалены" not in text and "ушли в историю" not in text, text


def test_save_tracking_schema_declares_replace():
    from memory_compiler.tools import list_tools
    tool = {t.name: t for t in asyncio.run(list_tools())}["save_tracking"]
    props = tool.inputSchema["properties"]
    assert props["replace"]["type"] == "boolean"
    assert "replace" not in (tool.inputSchema.get("required") or [])
    assert "null" in props["facts"]["description"]


# ─── Ветка тега release в save_lesson (v1.93.0) ──────────────────────────


def test_release_lesson_keeps_other_fields_and_shows_dropped(knowledge_dir):
    """Ветка тега release пишет в трекер одну version. Со слиянием прочие поля трекера
    остаются, а поля релиза прежней версии уходят в историю — и сводка их называет,
    а не теряет молча."""
    from memory_compiler.handlers import save_lesson
    save_tracking_article("testproj", "release",
                          {"version": "1.20.0", "commit": "abc1234", "tag": "v1.20.0",
                           "repo": "github.com/example/app"})
    out = asyncio.run(save_lesson(
        "memory-compiler v1.20.1 — фикс IP-коллизии",
        "Пофикшено извлечение версии в release-ветке.",
        "testproj",
        ["release"],
    ))
    cur = load_tracking("testproj", "release")["current"]
    assert cur["version"] == "1.20.1"
    assert cur["repo"] == "github.com/example/app"
    assert "commit" not in cur and "tag" not in cur
    text = out[0].text
    assert "version: 1.20.0 → 1.20.1" in text, text
    assert "commit: abc1234 → —" in text, text
    assert "tag: v1.20.0 → —" in text, text


def test_non_string_key_survives_version_change():
    """YAML читает ключ «8080:» как int — смена версии не должна падать на k.lower()."""
    merged, dropped = _merge_tracking_facts({"version": "1.0.0", 8080: "open", "tag": "v1.0.0"},
                                            {"version": "1.1.0"}, replace=False)
    assert dropped == ["tag"]
    assert merged == {"version": "1.1.0", 8080: "open"}
