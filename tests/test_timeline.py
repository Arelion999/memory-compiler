"""Тесты timeline-слайдера версий (v1.24.0): ряд bi-temporal снимков tracking-статьи.

Стерегут: порядок (history по возрастанию, current последним с открытым интервалом),
отделение служебных ключей интервала от полей факта, стабильный порядок fields,
устойчивость к битому frontmatter и то, что обычная статья даёт ПУСТОЙ ряд, а не ошибку.
"""
import asyncio
import json

from memory_compiler.storage import tracking_timeline
from memory_compiler.api import web_timeline


class FakeRequest:
    def __init__(self, query=None, path=None):
        self.query_params = query or {}
        self.path_params = path or {}


def _json(resp):
    return json.loads(resp.body)


SAMPLE = {
    "type": "tracking",
    "project": "memory-compiler",
    "entity": "release",
    "current": {"version": "1.23.1", "tag": "v1.23.1", "since": "2026-07-18"},
    "history": [
        {"version": "1.20.1", "date": "2026-07-17", "from": "2026-07-17", "to": "2026-07-18"},
        {"version": "1.22.0", "tag": "v1.22.0", "from": "2026-07-18", "to": "2026-07-18"},
    ],
}


# ─── tracking_timeline ───────────────────────────────────────────────────────

def test_timeline_orders_history_then_current():
    tl = tracking_timeline(SAMPLE)
    versions = [s["facts"]["version"] for s in tl["snapshots"]]
    assert versions == ["1.20.1", "1.22.0", "1.23.1"], "current должен идти последним"
    assert tl["snapshots"][-1]["current"] is True
    assert all(s["current"] is False for s in tl["snapshots"][:-1])


def test_timeline_current_interval_is_open():
    """У текущего снимка конец интервала не задан — факт действует «до сих пор»."""
    tl = tracking_timeline(SAMPLE)
    cur = tl["snapshots"][-1]
    assert cur["to"] is None
    assert cur["from"] == "2026-07-18", "from берётся из since, если from нет"


def test_timeline_interval_keys_are_not_facts():
    """from/to/since — описание интервала, а не поле факта: в facts их быть не должно."""
    tl = tracking_timeline(SAMPLE)
    for snap in tl["snapshots"]:
        assert not {"from", "to", "since"} & set(snap["facts"])


def test_timeline_keeps_date_as_fact():
    """`date` есть не у всех снимков и означает «когда факт зафиксирован» — это факт."""
    tl = tracking_timeline(SAMPLE)
    assert tl["snapshots"][0]["facts"]["date"] == "2026-07-17"


def test_timeline_fields_union_in_first_seen_order():
    tl = tracking_timeline(SAMPLE)
    assert tl["fields"] == ["version", "date", "tag"]
    assert tl["entity"] == "release"


def test_timeline_survives_broken_frontmatter():
    assert tracking_timeline({})["snapshots"] == []
    assert tracking_timeline({"history": "не список"})["snapshots"] == []
    assert tracking_timeline({"current": "строка"})["snapshots"] == []
    # мусорные элементы истории пропускаются, валидные — нет
    tl = tracking_timeline({"history": ["мусор", {"version": "1.0"}]})
    assert [s["facts"]["version"] for s in tl["snapshots"]] == ["1.0"]


def test_timeline_history_only_without_current():
    tl = tracking_timeline({"history": [{"version": "1.0", "from": "2026-01-01", "to": "2026-02-01"}]})
    assert len(tl["snapshots"]) == 1
    assert tl["snapshots"][0]["current"] is False
    assert tl["snapshots"][0]["to"] == "2026-02-01"


def test_timeline_coerces_yaml_dates_to_strings():
    """Регресс: YAML отдаёт `since: 2026-07-18` как datetime.date, JSONResponse такое
    не сериализует — endpoint падал 500 на реальной статье (на строковых датах не видно)."""
    from datetime import date
    tl = tracking_timeline({
        "current": {"version": "2.0", "since": date(2026, 7, 18), "released": date(2026, 7, 17)},
        "history": [{"version": "1.0", "from": date(2026, 7, 1), "to": date(2026, 7, 18)}],
    })
    assert tl["snapshots"][0]["from"] == "2026-07-01"
    assert tl["snapshots"][0]["to"] == "2026-07-18"
    assert tl["snapshots"][-1]["from"] == "2026-07-18"
    assert tl["snapshots"][-1]["facts"]["released"] == "2026-07-17"
    json.dumps(tl)  # не должно бросать


def test_timeline_does_not_mutate_input():
    import copy
    snapshot = copy.deepcopy(SAMPLE)
    tracking_timeline(SAMPLE)
    assert SAMPLE == snapshot


# ─── /api/timeline ───────────────────────────────────────────────────────────

def _write_tracking(kd, project="testproj", name="tracking_release.md"):
    p = kd / project
    p.mkdir(exist_ok=True)
    (p / name).write_text(
        "---\n"
        "type: tracking\n"
        "project: testproj\n"
        "entity: release\n"
        "current:\n"
        "  version: 2.0.0\n"
        "  since: 2026-07-18\n"
        "history:\n"
        "  - version: 1.0.0\n"
        "    from: 2026-07-01\n"
        "    to: 2026-07-18\n"
        "---\n\n"
        "# testproj — current state (release)\n\nтело\n",
        encoding="utf-8",
    )


def test_web_timeline_returns_snapshots(knowledge_dir):
    _write_tracking(knowledge_dir)
    data = _json(asyncio.run(web_timeline(
        FakeRequest(query={"project": "testproj", "file": "tracking_release.md"}))))
    assert data["entity"] == "release"
    assert [s["facts"]["version"] for s in data["snapshots"]] == ["1.0.0", "2.0.0"]
    assert data["snapshots"][-1]["current"] is True


def test_web_timeline_plain_article_gives_empty_series(knowledge_dir):
    """Обычная статья — пустой ряд, а не ошибка: UI зовёт endpoint на любое раскрытие."""
    p = knowledge_dir / "testproj"
    p.mkdir(exist_ok=True)
    (p / "plain.md").write_text("# Обычная\n\n**Теги:** test\n\n## Записи\nтело\n", encoding="utf-8")
    data = _json(asyncio.run(web_timeline(
        FakeRequest(query={"project": "testproj", "file": "plain.md"}))))
    assert data["snapshots"] == []


def test_web_timeline_missing_file_gives_empty_series(knowledge_dir):
    data = _json(asyncio.run(web_timeline(
        FakeRequest(query={"project": "testproj", "file": "нет_такого.md"}))))
    assert data["snapshots"] == []


def test_web_timeline_requires_params():
    assert _json(asyncio.run(web_timeline(FakeRequest(query={}))))["snapshots"] == []


def test_web_timeline_rejects_traversal():
    resp = asyncio.run(web_timeline(
        FakeRequest(query={"project": "testproj", "file": "../../etc/passwd"})))
    assert resp.status_code == 404
