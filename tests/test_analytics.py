"""Метрики качества памяти из аудит-лога (v1.57.0)."""

import json
import time
from datetime import datetime

import pytest

from memory_compiler import analytics


def _write_log(tmp_path, rows):
    (tmp_path / "_audit.log").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    return tmp_path


def _row(offset_sec, tool, size=8000, **args):
    ts = datetime.fromtimestamp(time.time() + offset_sec).strftime(analytics.AUDIT_TS_FMT)
    return {"ts": ts, "tool": tool, "args": args, "size": size}


@pytest.fixture
def audit(tmp_path, monkeypatch):
    """KNOWLEDGE_DIR импортирован в модуль ПО ЗНАЧЕНИЮ — патчим у него, а не у config."""
    monkeypatch.setattr(analytics, "KNOWLEDGE_DIR", tmp_path)
    return lambda rows: _write_log(tmp_path, rows)


def test_empty_log_gives_zeros_not_crash(audit):
    audit([])
    q = analytics.quality(24)
    assert q["searches"] == 0 and q["miss_rate"] == 0.0 and q["miss_queries"] == []


def test_missing_file_is_survivable(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, "KNOWLEDGE_DIR", tmp_path / "нет-такого")
    assert analytics.quality(24)["calls"] == 0


def test_miss_detected_by_response_size(audit):
    audit([
        _row(-600, "search", size=40, query="softwent пароль"),
        _row(-500, "search", size=9000, query="нормальный запрос"),
    ])
    q = analytics.quality(24)
    assert q["searches"] == 2
    assert q["misses"] == 1 and q["miss_rate"] == 0.5
    assert q["miss_queries"][0]["query"] == "softwent пароль"


def test_read_after_search_counts_as_useful(audit):
    audit([
        _row(-600, "search", query="что-то"),
        _row(-580, "read_article", filename="a.md"),
    ])
    q = analytics.quality(24)
    assert q["followed"] == 1 and q["follow_rate"] == 1.0
    assert q["reformulations"] == 0


def test_second_search_without_read_is_reformulation(audit):
    audit([
        _row(-600, "search", query="первая попытка"),
        _row(-570, "search", query="вторая попытка"),
    ])
    q = analytics.quality(24)
    assert q["reformulations"] == 1
    assert q["followed"] == 0


def test_read_long_after_search_is_not_credited(audit):
    """Окно связки узкое: чтение через час — уже другая работа, не следствие."""
    audit([
        _row(-7200, "search", query="давний запрос"),
        _row(-60, "read_article", filename="a.md"),
    ])
    assert analytics.quality(24)["followed"] == 0


def test_period_filter_cuts_old_rows(audit):
    audit([
        _row(-72 * 3600, "search", query="старый"),
        _row(-600, "search", query="свежий"),
    ])
    assert analytics.quality(1)["searches"] == 1
    assert analytics.quality(168)["searches"] == 2


def test_writes_and_projects_are_counted(audit):
    audit([
        _row(-600, "save_lesson", project="infra", topic="A"),
        _row(-500, "finish_task", project="infra", topic="B"),
        _row(-400, "edit_article", project="general", filename="c.md"),
        _row(-300, "search", query="не запись"),
    ])
    q = analytics.quality(24)
    assert q["writes"] == 3
    assert q["projects"][0] == ("infra", 2)


def test_broken_lines_are_skipped(audit, tmp_path):
    audit([_row(-600, "search", query="ок")])
    with (tmp_path / "_audit.log").open("a", encoding="utf-8") as f:
        f.write("не json\n{битый\n")
    assert analytics.quality(24)["searches"] == 1
