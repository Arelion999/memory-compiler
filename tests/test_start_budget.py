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

from memory_compiler import handlers, handlers_sessions


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


# ── потолок выдачи и честность заголовка (v1.89.0) ──────────────────────────
# Замер 20.09.2026 по транскриптам, 164 вызова start_task за 7 дней (871 тыс.
# символов): медиана 5362, у потолка 6000 — половина вызовов. Состав: «Найдено»
# 34,4%, открытые вопросы 23,2%, сессия с шапкой 20,5%, связанные действия
# 11,5%, факты 6,8%, runbooks 2,3%, сроки 1,2%.
#
# ⚠️ УБРАТЬ БЛОК — НЕ ЗНАЧИТ СЭКОНОМИТЬ. Бюджет раздаётся water-fill'ом:
# освободившееся место достаётся голодным соседям, а голодны они у половины
# вызовов. Поэтому экономия достигается ТОЛЬКО снижением самого потолка, и
# тест сторожит именно размер ответа, а не значение константы.

@pytest.fixture
def start_base(tmp_path, monkeypatch):
    """Проект, где стартовый контекст заведомо перерастает любой потолок."""
    import memory_compiler.config as cfg
    from memory_compiler import storage, handlers_sessions
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PROJECTS", ["demo"])
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(handlers_sessions, "KNOWLEDGE_DIR", tmp_path)
    (tmp_path / "demo").mkdir()

    # ⚠️ Длина берётся ДЛИНОЙ СТРОК, а не их числом: превью находки режется по
    # четвёртой строке, и «сорок коротких строк» давали блок в 150 символов —
    # тест зеленел на любом потолке, ничего не проверяя.
    async def _fat(*_a, **_kw):
        return [{"project": "demo", "file": "a%d.md" % i, "title": "Статья %d" % i,
                 "preview": "\n".join("контекст про сертификат %d — %s" % (i, "деталь " * 300)
                                      for _ in range(4)),
                 "score": 90.0 - i} for i in range(5)]
    monkeypatch.setattr(handlers, "_whoosh_async", _fat)
    return tmp_path


def test_crowded_start_gives_out_no_more_than_the_new_ceiling():
    """Голодный старт отдаёт не больше нового потолка.

    Размеры блоков — вдвое от замеренных средних (p90 выдачи 6151, то есть
    такие вызовы реальны). Проверяется РАЗДАЧА, а не длина готового текста:
    длина зависит ещё и от гранулярности резки — пункт режется по границе
    строки, и на длинных строках ответ выходит заметно короче выданного
    бюджета. Тест на длину текста поэтому зеленел бы и на потолке 6000.
    """
    want = [3648, 2474, 1346, 1052, 558, 262]        # найдено, вопросы, активность, факты, runbooks, сроки
    weight = [2.5, 3.0, 1.5, 1.5, 0.5, 2.0]
    got = handlers._weighted_budgets(want, weight, handlers_sessions.START_BUDGET)
    assert sum(got) <= 4000, "стартовый контекст отдаёт %d символов" % sum(got)
    # ⚠️ Позитивный контроль: экономия не должна съедать главное — иначе
    # «уложились в потолок» достигалось бы обнулением вопросов и находок.
    assert got[0] > 900 and got[1] > 900, ("находки и вопросы обязаны остаться "
                                           "читаемыми: %r" % (got,))


@pytest.mark.asyncio
async def test_found_block_does_not_promise_rerank_when_disabled(start_base):
    """Реранкер выключен с v1.27.0, rerank_score не проставляется — обещать его в
    заголовке значит врать модели о том, чем отобраны находки."""
    from memory_compiler import handlers_search
    assert not handlers_search.RERANK_ENABLED, "тест о выключенном реранкере"
    res = await handlers.start_task("сертификат домена продлить", "demo")
    header = next(l for l in res[0].text.splitlines() if l.startswith("## Найдено"))
    assert "rerank" not in header.lower(), header
    assert "hybrid" in header.lower(), header
