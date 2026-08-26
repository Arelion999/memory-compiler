"""Бюджет стартового контекста: water-fill по приоритетам (v1.65.0).

До этого каждый блок `start_task` резался своим лимитом в символах — сессия 1800,
вопрос 300, факт 220, compact 600, решение 100 — и лимиты не знали друг о друге.
Замер 2026-08-26 по боевой базе (48 проектов, аудит 8038 вызовов):

* 50% показанных открытых вопросов (18 из 36) обрезались по 300 символов,
  ещё 12 вопросов не показывались вовсе; при этом
* у 28 проектов из 46 весь стартовый контекст не дотягивал и до 1500 символов —
  то есть место было, а блок всё равно резался;
* размер ответа start_task гулял от 457 до 6808 символов, общего потолка не было
  вовсе — он складывался стихийно из суммы независимых срезов.

Water-fill решает обе стороны: короткий блок берёт своё целиком, неиспользованное
достаётся тем, кому не хватило, а сумма ограничена сверху одним числом.
"""

import re

import pytest

from memory_compiler import handlers


# ── раздача бюджета ─────────────────────────────────────────────────────────

def test_short_blocks_are_given_in_full():
    got = handlers._weighted_budgets([100, 200, 50], [1, 1, 1], total=6000, floor=10)
    assert got == [100, 200, 50], "влезающий блок обязан получить свою длину, не долю"


def test_unused_budget_goes_to_the_hungry_block():
    """Суть правки: одинокий длинный блок получает бюджет соседей, а не свой лимит."""
    alone = handlers._weighted_budgets([5000, 0, 0], [1, 1, 1], total=3000, floor=10)
    crowd = handlers._weighted_budgets([5000, 3000, 3000], [1, 1, 1], total=3000, floor=10)
    assert alone[0] == 3000, "при пустых соседях блок берёт весь бюджет"
    assert crowd[0] < alone[0], "при конкуренции — долю"


def test_priority_decides_who_gets_cut_first():
    """При равной длине больший вес получает больше — это и есть «по приоритетам»."""
    got = handlers._weighted_budgets([4000, 4000], [3.0, 1.0], total=4000, floor=10)
    assert got[0] > got[1]
    assert sum(got) <= 4000


def test_total_never_exceeds_budget():
    got = handlers._weighted_budgets([9000, 9000, 9000], [3, 2, 1], total=1000, floor=10)
    assert sum(got) <= 1000


def test_scraps_are_dropped_and_their_share_returned():
    """Блок, которому досталось меньше «полезного минимума», не показывается вовсе,
    а его доля уходит остальным: обрывок в 30 символов не контекст, а шум."""
    got = handlers._weighted_budgets([2000, 2000, 2000], [10.0, 10.0, 0.01], total=1000, floor=200)
    assert got[2] == 0, "огрызок обязан быть отброшен"
    assert sum(got[:2]) == 1000, "его доля возвращается в пул, а не теряется"


def test_result_does_not_depend_on_block_order():
    a = handlers._weighted_budgets([5000, 100, 300], [1, 2, 3], total=1200, floor=10)
    b = handlers._weighted_budgets([300, 100, 5000], [3, 2, 1], total=1200, floor=10)
    assert sorted(a) == sorted(b), "раздача обязана быть независимой от порядка"


def test_zero_length_blocks_take_nothing():
    got = handlers._weighted_budgets([0, 0, 500], [1, 1, 1], total=1000, floor=10)
    assert got[:2] == [0, 0] and got[2] == 500


# ── сборка блоков ───────────────────────────────────────────────────────────

def test_block_keeps_whole_items_while_they_fit():
    block = handlers._Block("q", "## Вопросы", ["раз" * 10, "два" * 10], weight=1)
    out = handlers._render_block(block, budget=200)
    assert "раз" * 10 in out and "два" * 10 in out


def test_hidden_items_are_counted_not_swallowed():
    """Молчаливая обрезка читается как «это весь контекст». Скрытое обязано быть
    названо числом — иначе следующая сессия не узнает, что смотрит на огрызок."""
    block = handlers._Block("q", "## Вопросы", ["а" * 300, "б" * 300, "в" * 300], weight=1)
    out = handlers._render_block(block, budget=320)
    assert re.search(r"ещё \d+", out), "не показанные пункты должны быть посчитаны"


def test_empty_block_renders_nothing():
    assert handlers._render_block(handlers._Block("q", "## Вопросы", [], weight=1), 500) == ""
