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
    assert q["acted"] == 1 and q["act_rate"] == 1.0
    assert q["chained"] == 0


def test_second_search_without_action_is_only_chained(audit):
    """Раньше это называлось переформулировкой и шло в вердикт о ранжировании.
    Теперь — наблюдаемый факт без толкования: замер показал, что подряд идущие
    поиски почти всегда про РАЗНОЕ (похожих 6 пар из 116)."""
    audit([
        _row(-600, "search", query="первая попытка"),
        _row(-570, "search", query="вторая попытка"),
    ])
    q = analytics.quality(24)
    assert q["chained"] == 1
    assert q["acted"] == 0


def test_read_long_after_search_is_not_credited(audit):
    """Окно связки узкое: чтение через час — уже другая работа, не следствие."""
    audit([
        _row(-7200, "search", query="давний запрос"),
        _row(-60, "read_article", filename="a.md"),
    ])
    assert analytics.quality(24)["acted"] == 0


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


# ── честность метрик (v1.66.0) ──────────────────────────────────────────────
# Замер 26.08.2026 по боевому логу (525 поисков за месяц) показал, что обе
# метрики полезности считали успех провалом:
#   за поиском идёт чтение статьи 51%, ЗАПИСЬ в базу 17%, ещё поиск 22%,
#   ничего 9%. Прежний follow_rate засчитывал только чтение и объявлял провалом
#   случай «ответ нашёлся прямо в превью, пошёл писать» — а выдача поиска весит
#   13 КБ, там он и находится.
#   reformulations считал переформулировкой ЛЮБОЙ следующий поиск в окне: из 116
#   таких пар текстово похожи (Jaccard ≥ 0.4) лишь 6, остальные — сбор контекста
#   по разным подтемам. Завышение в 19 раз.

def test_write_after_search_counts_as_useful(audit):
    """Ответ найден в превью и сразу записан — это успех поиска, а не промах."""
    audit([
        _row(-600, "search", size=9000, query="как настроен nginx"),
        _row(-580, "save_lesson", size=70, project="infra", topic="nginx"),
    ])
    q = analytics.quality(24)
    assert q["acted"] == 1, "запись после поиска — действие по выдаче"
    assert q["act_rate"] == 1.0


def test_read_after_search_still_counts(audit):
    """Позитивный контроль: прежний сигнал никуда не делся."""
    audit([
        _row(-600, "search", size=9000, query="пароль от роутера"),
        _row(-590, "read_article", size=2000, project="infra", filename="a.md"),
    ])
    assert analytics.quality(24)["acted"] == 1


def test_search_without_any_action_is_not_counted_as_useful(audit):
    audit([
        _row(-600, "search", size=9000, query="первый"),
        _row(-590, "search", size=9000, query="совершенно другая тема"),
    ])
    q = analytics.quality(24)
    assert q["acted"] == 0
    assert q["chained"] == 1, "за поиском сразу поиск — это наблюдаемый факт"


def test_chained_search_is_not_called_a_reformulation(audit):
    """Ключ правки: сбор контекста по разным подтемам — не признак промаха.

    Естественного порога по схожести не существует (замер: распределение
    Jaccard монотонно убывает, 50% пар вообще без общих слов), поэтому метрика
    называет наблюдаемое — «за поиском пошёл поиск» — и не выдаёт вердикт о
    ранжировании.
    """
    audit([
        _row(-600, "search", size=9000, query="pve121 гипервизор"),
        _row(-560, "search", size=9000, query="Хабаровская площадка сети"),
        _row(-520, "search", size=9000, query="DC4 адреса виртуалок"),
    ])
    q = analytics.quality(24)
    assert "reformulations" not in q, "метрика, выдающая намерение за факт, убрана"
    assert q["chained"] == 2


def test_context_volume_is_reported_per_tool(audit):
    """Кто съедает контекст — теперь видно: search весит на порядок больше
    остальных ответов, и без этой строки приоритеты ставились вслепую."""
    audit([
        _row(-600, "search", size=13000, query="раз"),
        _row(-590, "read_article", size=2400, project="p", filename="a.md"),
        _row(-580, "finish_task", size=190, project="p", topic="t"),
    ])
    q = analytics.quality(24)
    top = dict(q["context_bytes"])
    assert top["search"] == 13000 and top["read_article"] == 2400
    assert q["context_total"] == 15590


def test_action_is_credited_to_the_search_it_actually_followed(audit):
    """Одно чтение не должно засчитываться СРАЗУ НЕСКОЛЬКИМ поискам.

    `any(...)` по окну именно это и делал: поиск, за которым сразу пошёл другой
    поиск, всё равно получал зачёт за чтение, случившееся уже после второго.
    Сверка на боевом логе: по окну выходило 85% полезных поисков, по первому
    следующему событию — 68%. Завышение на 88 поисков из 525.
    """
    audit([
        _row(-600, "search", size=9000, query="первый"),
        _row(-570, "search", size=9000, query="второй"),
        _row(-560, "read_article", size=2000, filename="a.md"),
    ])
    q = analytics.quality(24)
    assert q["acted"] == 1, "зачёт положен только второму поиску"
    assert q["chained"] == 1, "первый поиск не привёл к действию"


# ── суточный замер (v1.73.0) ────────────────────────────────────────────────
# Контрольный замер после релизов зависел от того, вспомнит ли о нём человек, и
# ровно поэтому не делался: тот же случай, что со `stale_facts` — ноль вызовов
# за четыре с половиной месяца. Считает СЕРВЕР, раз в сутки, по тому же
# аудит-логу.
#
# ⚠️ Существующие формулы не дублируются: daily() берёт их у quality(). Копия
# расчёта — это уже случившаяся болезнь (act_rate живёт в трёх местах сразу:
# analytics, панель ui.py и хук mc_guard.py), четвёртую заводить нельзя.

def test_daily_survives_empty_log(audit):
    audit([])
    d = analytics.daily(24)
    assert d["searches"] == 0 and d["sessions"] == 0 and d["blind_rate"] == 0.0


def test_daily_counts_blind_sessions(audit):
    """Сессия, начатая НЕ с загрузки контекста, считается слепой.

    Аудит не пишет session_id, поэтому сессии режутся по паузе — это
    эвристика, и она честно названа в docstring функции.
    """
    gap = analytics.SESSION_GAP_SEC + 60
    audit([
        _row(-3 * gap, "start_task", project="demo"),          # зрячая
        _row(-3 * gap + 30, "save_lesson", project="demo"),
        _row(-2 * gap, "save_lesson", project="demo"),         # слепая
        _row(-2 * gap + 30, "finish_task", project="demo"),
        _row(-gap, "search", query="что-то"),                  # слепая
    ])
    d = analytics.daily(24)
    assert d["sessions"] == 3, "три серии, разделённые паузой"
    assert d["blind"] == 2
    assert d["blind_rate"] == round(2 / 3, 3)


def test_daily_counts_finish_task_without_summary(audit):
    audit([
        _row(-600, "finish_task", topic="a", session_summary="итог"),
        _row(-500, "finish_task", topic="b"),
        _row(-400, "finish_task", topic="c", session_summary="  "),
    ])
    d = analytics.daily(24)
    assert d["finish_total"] == 3
    assert d["finish_no_summary"] == 2, "пустая строка — тоже без сводки"


def test_daily_reports_search_volume(audit):
    """Медиана выдачи поиска и доля поиска в отданных символах — то, ради чего
    вводился SEARCH_BUDGET; без них эффект правки не виден."""
    audit([
        _row(-900, "search", size=6000, query="a"),
        _row(-800, "search", size=7000, query="b"),
        _row(-700, "search", size=8000, query="c"),
        _row(-600, "start_task", size=3000, project="demo"),
    ])
    d = analytics.daily(24)
    assert d["search_median"] == 7000
    assert d["search_share"] == round(21000 / 24000, 3)


def test_daily_counts_session_notes(audit):
    audit([
        _row(-900, "session_note", note="раз", project="demo"),
        _row(-800, "session_note", note="два", project="demo"),
        _row(-700, "search", query="a"),
    ])
    assert analytics.daily(24)["notes"] == 2


def test_daily_reuses_quality_numbers(audit):
    """Позитивный контроль против копии расчёта: act_rate у daily() обязан
    совпадать с quality() на тех же данных — если формулу продублируют, они
    разъедутся молча."""
    rows = [
        _row(-900, "search", size=9000, query="a"),
        _row(-880, "read_article", project="demo"),
        _row(-600, "search", size=40, query="пусто"),
    ]
    audit(rows)
    assert analytics.daily(24)["act_rate"] == analytics.quality(24)["act_rate"]
    assert analytics.daily(24)["miss_rate"] == analytics.quality(24)["miss_rate"]


def test_daily_also_counts_context_anywhere_in_the_session(audit):
    """Двух цифр требует сама эвристика нарезки.

    Длинная непрерывная работа режется паузами на несколько серий, и все
    продолжения по определению начинаются НЕ со start_task — «вслепую 100%»
    получается артефактом, а не фактом. Исходный замер 26.08 поэтому и давал
    пару: контекст где-либо (54%) и контекст первым вызовом (21%).
    """
    gap = analytics.SESSION_GAP_SEC + 60
    audit([
        _row(-2 * gap, "save_lesson", project="demo"),      # начата вслепую,
        _row(-2 * gap + 30, "start_task", project="demo"),  # но контекст всё же взят
        _row(-gap, "search", query="что-то"),               # и начата вслепую, и без контекста
    ])
    d = analytics.daily(24)
    assert d["blind"] == 2, "обе серии начаты не с контекста"
    assert d["ctx_anywhere"] == 1, "в одной из них контекст всё же загружали"
    assert d["ctx_rate"] == 0.5
