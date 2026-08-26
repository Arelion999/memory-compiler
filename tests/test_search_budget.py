"""Бюджет выдачи search: убывающее превью по позиции (v1.67.0).

Замер 26.08.2026 по боевому логу: `search` отдаёт 64% всех символов, которые
инструменты возвращают за неделю (2626 тыс. из 4125 тыс.), медиана выдачи 13132
символа, p90 16885. Механика — 8 результатов, у КАЖДОГО превью в 10 строк,
одинаково для первого и восьмого.

Два независимых замера говорят, что хвост столько не стоит:

* по baseline retrieval_eval: recall@3 = 0.667, recall@5 = 0.78, recall@10 = 0.84
  — позиции 6-8 добавляют около 6% попаданий, занимая примерно 37% объёма;
* по 345 парам «запрос → открытая статья»: слова запроса встречаются в СТРОКЕ 0
  (заголовке) у 76% пар, в первых трёх строках — у 87%, в первых четырёх — у 91%.
  Строки 5-10 добавляют 9%.

Поэтому первым позициям остаётся полное превью, хвосту — короткое, а вся выдача
ограничена общим потолком. Порядок и состав результатов НЕ меняются: правка
про рендер, а не про ранжирование.
"""

import pytest

from memory_compiler import handlers


def _res(i, lines=12, project="infra", score=50):
    body = "\n".join([f"# Заголовок статьи {i}"] + [f"строка {i}.{n} " + "ы" * 60
                                                    for n in range(lines)])
    return {"project": project, "file": f"a{i}.md", "title": f"Заголовок статьи {i}",
            "score": score - i, "preview": body}


def test_head_gets_more_than_tail_when_budget_is_tight():
    """При нехватке места голова сохраняет превью, хвост ужимается. В этом правка.

    ⚠️ Проверять надо именно ДЕФИЦИТ: боевая выдача весит 13132 символа при
    потолке 7000, то есть режется всегда. Если превью короткие и влезают все,
    резать нечего — требовать перекоса в этом случае неверно, за тот случай
    отвечает соседний тест.
    """
    out = handlers._render_search_results([_res(i, lines=24) for i in range(8)])
    blocks = out.split("---\n")[1:]
    head, tail = len(blocks[0]), len(blocks[-1])
    assert head > tail * 2, f"голова {head} должна быть заметно длиннее хвоста {tail}"


def test_nothing_is_cut_when_everything_fits():
    """Позитивный контроль: бюджет не режет ради самого дележа."""
    results = [_res(i, lines=4) for i in range(8)]
    out = handlers._render_search_results(results)
    for r in results:
        assert r["preview"] in out, "влезающее превью обязано дойти целиком"


def test_total_stays_within_budget():
    out = handlers._render_search_results([_res(i, lines=40) for i in range(8)])
    assert len(out) <= handlers.SEARCH_BUDGET, f"выдача {len(out)} превысила потолок"


def test_every_result_keeps_its_title():
    """Хвост можно сжать, но не до безымянности: 76% сигнала — в заголовке."""
    out = handlers._render_search_results([_res(i, lines=40) for i in range(8)])
    for i in range(8):
        assert f"Заголовок статьи {i}" in out, f"позиция {i} потеряла заголовок"


def test_order_and_composition_are_untouched():
    """Правка про рендер, а не про ранжирование: порядок обязан остаться."""
    out = handlers._render_search_results([_res(i) for i in range(8)])
    positions = [out.index(f"Заголовок статьи {i}") for i in range(8)]
    assert positions == sorted(positions), "порядок результатов изменился"


def test_short_results_are_not_padded():
    """Короткие превью отдаются целиком — бюджет не обязывает добирать объём."""
    small = [{"project": "p", "file": "s.md", "title": "Коротко",
              "score": 40, "preview": "# Коротко\nодна строка"} for _ in range(3)]
    out = handlers._render_search_results(small)
    assert out.count("одна строка") == 3
    assert len(out) < 600


def test_unused_budget_flows_to_the_others():
    """Место, не занятое короткими результатами, достаётся длинным."""
    mixed = [_res(0, lines=40)] + [{"project": "p", "file": f"s{i}.md",
                                    "title": f"Коротко {i}", "score": 30,
                                    "preview": f"# Коротко {i}\nодна строка"}
                                   for i in range(1, 8)]
    alone = handlers._render_search_results(mixed)
    crowded = handlers._render_search_results([_res(i, lines=40) for i in range(8)])
    first_alone = len(alone.split("---\n")[1])
    first_crowded = len(crowded.split("---\n")[1])
    assert first_alone > first_crowded, "неиспользованный бюджет должен перетекать"


def test_secret_placeholder_survives_the_cut():
    """У секрета в превью стоит подсказка вместо тела — обрезать её нельзя,
    иначе выдача выглядит пустой строкой без объяснения."""
    secret = {"project": "niksdesk", "file": "secret_x.md", "title": "Доступы X",
              "score": 90,
              "preview": "# Доступы X\n\n[зашифровано — используй read_article для просмотра]"}
    out = handlers._render_search_results([secret] + [_res(i, lines=40) for i in range(7)])
    assert "[зашифровано" in out


# ── подбор строк по запросу (замер вытеснил «первые N строк») ────────────────
# Сжатие хвоста первыми строками теряло сигнал: слова запроса оставались в блоке
# целевой статьи у 74% пар против 81% при полном превью (−7 п.п.). Подбор строк
# ПО ЗАПРОСУ в том же бюджете даёт 79% на хвосте и 82% в голове — то есть
# возвращает почти всё, ничего не добавляя к объёму. Замер: 418 golden-пар
# «запрос → открытая статья», позиции 1, 4 и 8.

def test_line_with_query_words_beats_the_first_line():
    """При тесном бюджете показываем строку СО СЛОВАМИ ЗАПРОСА, а не просто первую."""
    res = [{"project": "p", "file": "a.md", "title": "Статья про инфраструктуру",
            "score": 50,
            "preview": "# Статья про инфраструктуру\n" + "пустая строка " * 30
                       + "\nпароль от коммутатора лежит в сейфе\n"
                       + "ещё строка " * 30}]
    out = handlers._render_search_results(res, "# Поиск\n", query="пароль коммутатора")
    assert "пароль от коммутатора" in out


def test_selected_lines_keep_their_original_order():
    """Строки не перетасовываются: превью читают как текст, а не как набор цитат."""
    res = [{"project": "p", "file": "a.md", "title": "Заголовок",
            "score": 50,
            "preview": "# Заголовок\nальфа " + "х" * 200 + "\nбета nginx\nгамма nginx"}]
    out = handlers._render_search_results(res, "", query="nginx")
    assert out.index("бета nginx") < out.index("гамма nginx")


def test_query_is_optional():
    """Без запроса рендер обязан работать по-старому — им пользуются и другие места."""
    out = handlers._render_search_results([_res(0, lines=24)], "")
    assert "Заголовок статьи 0" in out
