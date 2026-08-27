"""Суточный замер, который делает себя сам (v1.73.0).

Контрольный замер после релизов v1.65–v1.72 не был сделан ни разу: он зависел
от того, вспомнит ли о нём человек. Прецедент рядом — `stale_facts` и
`knowledge_gap` за 4.5 месяца не позвали ни разу. Поэтому замер считает сервер
по расписанию и сам кладёт результат в базу.
"""

import json

import pytest

from memory_compiler import daily_metrics


SNAP = {"hours": 24.0, "calls": 400, "searches": 40, "miss_rate": 0.02,
        "act_rate": 0.7, "chained": 5, "search_median": 6800, "search_share": 0.55,
        "notes": 6, "finish_total": 10, "finish_no_summary": 2, "no_summary_rate": 0.2,
        "sessions": 12, "blind": 6, "blind_rate": 0.5}


def test_report_states_the_numbers_a_human_will_compare():
    text = daily_metrics.format_report(SNAP, None)
    for needle in ("6800", "40", "6", "12"):
        assert needle in text, "в отчёте нет цифры %s" % needle
    assert "MTR " in text, "машиночитаемый снимок нужен следующему запуску для дельт"


def test_machine_snapshot_round_trips():
    """Следующий запуск читает свою же строку — иначе дельт не будет никогда."""
    text = daily_metrics.format_report(SNAP, None)
    assert daily_metrics.parse_snapshot(text) == SNAP


def test_second_run_shows_movement():
    prev = dict(SNAP, search_median=14287, blind_rate=0.81)
    text = daily_metrics.format_report(SNAP, prev)
    assert "14287" in text and "6800" in text, "обе точки должны быть видны"
    assert "→" in text, "сдвиг показывается стрелкой, иначе ряд не читается"


def test_first_run_without_history_does_not_crash():
    text = daily_metrics.format_report(SNAP, None)
    assert text and "MTR " in text


def test_snapshot_is_taken_from_the_last_record_not_the_first():
    """В статье накапливаются записи; сравнивать надо с ПОСЛЕДНЕЙ."""
    older = daily_metrics.format_report(dict(SNAP, search_median=14287), None)
    newer = daily_metrics.format_report(dict(SNAP, search_median=6800), None)
    article = "# Статья\n\n## Записи\n\n### день 1\n%s\n\n### день 2\n%s\n" % (older, newer)
    assert daily_metrics.parse_snapshot(article)["search_median"] == 6800


def test_broken_snapshot_line_is_survivable():
    """Порченая строка не должна ронять суточный прогон — замер важнее дельты."""
    assert daily_metrics.parse_snapshot("### день\nMTR {это не json\n") is None
    assert daily_metrics.parse_snapshot("никаких снимков тут нет") is None


# ── позитивный контроль против немого except ────────────────────────────────
# Первая редакция модуля звала несуществующую storage.slugify, и обёрнутый в
# `except Exception` вызов гасил ImportError: замер писался бы каждый день, а
# дельты не появились бы НИКОГДА и молча. Тест ходит по настоящему пути записи
# и чтения — на опечатке в имени он падает.

@pytest.mark.asyncio
async def test_written_report_is_found_back_by_the_next_run(knowledge_dir, monkeypatch):
    from memory_compiler.handlers import save_lesson

    monkeypatch.setattr(daily_metrics, "PROJECT", "testproj")
    text = daily_metrics.format_report(SNAP, None)
    await save_lesson(topic=daily_metrics.TOPIC, content=text,
                      project="testproj", tags=list(daily_metrics.TAGS))

    got = daily_metrics._previous_snapshot()
    assert got is not None, "прошлый замер не найден — ряд никогда не покажет сдвиг"
    assert got["search_median"] == SNAP["search_median"]


def test_missing_article_gives_no_snapshot_without_raising(knowledge_dir, monkeypatch):
    """Первый в жизни прогон: статьи ещё нет — это не ошибка."""
    monkeypatch.setattr(daily_metrics, "PROJECT", "testproj")
    assert daily_metrics._previous_snapshot() is None


# ── замер считается ВНУТРИ сервера (v1.73.0) ────────────────────────────────
# Отдельный процесс python в том же контейнере переписывал бы pickle эмбеддингов
# мимо внутрипроцессного лока — затирая то, что сервер посчитал за это время.
# Поэтому штатный путь — REST у работающего сервера.

@pytest.mark.asyncio
async def test_endpoint_measures_and_writes(knowledge_dir, monkeypatch):
    from memory_compiler.api import web_daily_metrics

    monkeypatch.setattr(daily_metrics, "PROJECT", "testproj")

    class Req:
        async def json(self):
            return {"hours": 24}

    resp = await web_daily_metrics(Req())
    assert resp.status_code == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert daily_metrics.SNAPSHOT_MARK in body["result"], "в ответе должен быть замер"
    assert daily_metrics._previous_snapshot() is not None, "замер обязан лечь в базу"


@pytest.mark.asyncio
async def test_endpoint_rejects_absurd_window(knowledge_dir):
    from memory_compiler.api import web_daily_metrics

    class Req:
        async def json(self):
            return {"hours": 100000}

    resp = await web_daily_metrics(Req())
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_defaults_to_a_day_when_body_is_empty(knowledge_dir, monkeypatch):
    """Cron может дёрнуть без тела — это не ошибка."""
    from memory_compiler.api import web_daily_metrics

    monkeypatch.setattr(daily_metrics, "PROJECT", "testproj")

    class Req:
        async def json(self):
            raise ValueError("нет тела")

    resp = await web_daily_metrics(Req())
    assert resp.status_code == 200
