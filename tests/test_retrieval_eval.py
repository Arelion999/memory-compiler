"""Тесты харнесса оценки retrieval (v1.26.0).

Метрика — инструмент принятия решений по ядру поиска, поэтому сама она должна быть
проверена: ошибка в подсчёте recall/MRR или в сборке golden-набора привела бы к
неверному выводу «стало лучше/хуже» и к изменению чанкования на ложных основаниях.
"""
import json

from memory_compiler.retrieval_eval import (
    filter_reachable, load_golden, compare, per_query_rr, format_comparison,
    build_golden, evaluate, filter_existing, build_known_item_set,
)


def _q(tool, **args):
    return {"tool": tool, "args": args, "ts": "2026-07-18 10:00:00"}


def _open(project, filename):
    return {"tool": "read_article", "args": {"project": project, "filename": filename},
            "ts": "2026-07-18 10:00:01"}


# ─── build_golden ────────────────────────────────────────────────────────────

def test_golden_pairs_search_with_following_opens():
    entries = [
        _q("search", query="nginx проксирование", project="work"),
        _open("work", "nginx.md"),
        _open("work", "proxy.md"),
    ]
    g = build_golden(entries)
    assert len(g) == 1
    assert g[0]["query"] == "nginx проксирование"
    assert g[0]["project"] == "work"
    assert g[0]["expected"] == {"work/nginx.md", "work/proxy.md"}


def test_golden_next_query_closes_previous():
    """Открытия после СЛЕДУЮЩЕГО запроса относятся уже к нему, а не к предыдущему."""
    entries = [
        _q("search", query="первый", project="work"),
        _open("work", "a.md"),
        _q("search", query="второй", project="work"),
        _open("work", "b.md"),
    ]
    g = build_golden(entries)
    assert [x["query"] for x in g] == ["первый", "второй"]
    assert g[0]["expected"] == {"work/a.md"}
    assert g[1]["expected"] == {"work/b.md"}


def test_golden_drops_queries_without_opens():
    """Нет открытия — нет ground truth: такой запрос в выборку не берём."""
    entries = [
        _q("search", query="без результата", project="work"),
        _q("search", query="с результатом", project="work"),
        _open("work", "a.md"),
    ]
    g = build_golden(entries)
    assert [x["query"] for x in g] == ["с результатом"]


def test_golden_ignores_start_task():
    """start_task ОТВЕРГНУТ как источник (замер 2026-07-19): он закрывал предыдущий
    запрос и переназначал клик с полноценного поискового запроса на описание задачи —
    набор становился шумнее. Клик должен остаться за поиском."""
    entries = [
        _q("search", query="бэкап NAS Synology offsite", project="infra"),
        _q("start_task", topic="Доработка макета НИКС_ПФ_MXL", project="infra"),
        _open("infra", "backup.md"),
    ]
    g = build_golden(entries)
    assert [x["query"] for x in g] == ["бэкап NAS Synology offsite"], \
        "start_task перехватил клик у настоящего поискового запроса"
    assert g[0]["expected"] == {"infra/backup.md"}


# ─── known-item набор ────────────────────────────────────────────────────────

def test_known_item_builds_title_to_article_pairs(knowledge_dir):
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "nginx.md").write_text("# Проксирование nginx на бэкенд\n\nтело\n", encoding="utf-8")
    items = build_known_item_set(knowledge_dir)
    mine = [i for i in items if i["expected"] == {"testproj/nginx.md"}]
    assert len(mine) == 1
    assert mine[0]["query"] == "Проксирование nginx на бэкенд"
    assert mine[0]["project"] == "testproj"


def test_known_item_skips_service_files_and_short_titles(knowledge_dir):
    """Служебные файлы — не статьи-ответы; слишком короткий заголовок даёт бессмысленный запрос."""
    proj = knowledge_dir / "testproj"
    proj.mkdir(exist_ok=True)
    (proj / "_log.md").write_text("# Журнал проекта тестового\n\nтело\n", encoding="utf-8")
    (proj / "short.md").write_text("# 1С\n\nтело\n", encoding="utf-8")
    names = {next(iter(i["expected"])) for i in build_known_item_set(knowledge_dir)}
    assert "testproj/_log.md" not in names, "служебный файл попал в набор"
    assert "testproj/short.md" not in names, "слишком короткий заголовок попал в набор"


def test_golden_accepts_ask_and_get_context():
    entries = [
        _q("ask", question="как настроить vpn", project="infra"),
        _open("infra", "vpn.md"),
        _q("get_context", query="деплой", project="infra"),
        _open("infra", "deploy.md"),
    ]
    g = build_golden(entries)
    assert [x["query"] for x in g] == ["как настроить vpn", "деплой"]


def test_golden_defaults_project_to_all():
    g = build_golden([_q("search", query="что-то"), _open("work", "a.md")])
    assert g[0]["project"] == "all"


def test_golden_caps_expected():
    entries = [_q("search", query="q", project="work")] + [
        _open("work", f"a{i}.md") for i in range(10)
    ]
    g = build_golden(entries, max_expected=3)
    assert len(g[0]["expected"]) == 3


def test_golden_ignores_malformed_entries():
    entries = [
        {"tool": "search", "args": "не словарь", "ts": "t"},
        _q("search", query="   ", project="work"),
        _open("work", "a.md"),
        _q("search", query="норм", project="work"),
        {"tool": "read_article", "args": {"project": "work"}, "ts": "t"},  # нет filename
        _open("work", "ok.md"),
    ]
    g = build_golden(entries)
    assert [x["query"] for x in g] == ["норм"]
    assert g[0]["expected"] == {"work/ok.md"}


# ─── filter_existing ─────────────────────────────────────────────────────────

def test_filter_existing_drops_deleted_articles(knowledge_dir):
    (knowledge_dir / "testproj").mkdir(exist_ok=True)
    (knowledge_dir / "testproj" / "alive.md").write_text("# A\n", encoding="utf-8")
    golden = [
        {"query": "q1", "project": "testproj", "expected": {"testproj/alive.md", "testproj/dead.md"}},
        {"query": "q2", "project": "testproj", "expected": {"testproj/dead.md"}},
    ]
    out = filter_existing(golden, knowledge_dir)
    assert len(out) == 1, "запрос, где все ожидания удалены, должен выпасть"
    assert out[0]["expected"] == {"testproj/alive.md"}


# ─── evaluate ────────────────────────────────────────────────────────────────

def test_evaluate_perfect_rank_one():
    golden = [{"query": "q", "project": "all", "expected": {"p/a.md"}}]
    res = evaluate(golden, lambda q, p, l: ["p/a.md", "p/b.md"])
    assert res["n"] == 1 and res["mrr"] == 1.0
    assert res["recall@1"] == 1.0 and res["recall@10"] == 1.0


def test_evaluate_rank_three():
    golden = [{"query": "q", "project": "all", "expected": {"p/c.md"}}]
    res = evaluate(golden, lambda q, p, l: ["p/a.md", "p/b.md", "p/c.md"])
    assert res["recall@1"] == 0.0
    assert res["recall@3"] == 1.0
    assert abs(res["mrr"] - 1 / 3) < 1e-4


def test_evaluate_miss_gives_zero():
    golden = [{"query": "q", "project": "all", "expected": {"p/z.md"}}]
    res = evaluate(golden, lambda q, p, l: ["p/a.md", "p/b.md"])
    assert res["mrr"] == 0.0 and res["recall@10"] == 0.0


def test_evaluate_averages_across_queries():
    golden = [
        {"query": "q1", "project": "all", "expected": {"p/a.md"}},   # ранг 1
        {"query": "q2", "project": "all", "expected": {"p/zzz.md"}},  # промах
    ]
    res = evaluate(golden, lambda q, p, l: ["p/a.md", "p/b.md"])
    assert res["n"] == 2
    assert res["recall@1"] == 0.5
    assert abs(res["mrr"] - 0.5) < 1e-4


def test_evaluate_respects_limit():
    """limit обрезает выдачу: попадание за пределом limit не засчитывается."""
    golden = [{"query": "q", "project": "all", "expected": {"p/c.md"}}]
    ranked = ["p/a.md", "p/b.md", "p/c.md"]
    assert evaluate(golden, lambda q, p, l: ranked, limit=2)["recall@10"] == 0.0
    assert evaluate(golden, lambda q, p, l: ranked, limit=3)["recall@10"] == 1.0


def test_evaluate_empty_golden():
    res = evaluate([], lambda q, p, l: [])
    assert res["n"] == 0 and res["mrr"] == 0.0


# ─── Отсев недостижимых ожиданий (v1.32.0) ───────────────────────────────────

def _scope(path, project):
    """Упрощённый in_search_scope: проект по префиксу пути, shared — везде."""
    return project == "all" or path.startswith(project + "/") or path.endswith("shared.md")


def test_filter_reachable_drops_out_of_scope():
    """Ожидание в ЧУЖОМ проекте недостижимо: скоуп запроса его исключает.

    Замер на живом логе (2026-07-19): 35 пар из 299 указывали вне скоупа —
    поиск не мог их вернуть физически, а метрика засчитывала это как промах."""
    golden = [{"query": "q", "project": "a", "expected": {"a/ok.md", "b/чужая.md"}}]
    out = filter_reachable(golden, _scope)
    assert out[0]["expected"] == {"a/ok.md"}


def test_filter_reachable_keeps_shared():
    """Кросс-проектная статья достижима из любого проекта — не отсеивать."""
    golden = [{"query": "q", "project": "a", "expected": {"b/shared.md"}}]
    assert filter_reachable(golden, _scope)[0]["expected"] == {"b/shared.md"}


def test_filter_reachable_drops_query_without_reachable_expectations():
    """Если ВСЕ ожидания вне скоупа, запрос выпадает целиком: измерять нечего."""
    golden = [
        {"query": "q1", "project": "a", "expected": {"b/x.md", "c/y.md"}},
        {"query": "q2", "project": "a", "expected": {"a/z.md"}},
    ]
    out = filter_reachable(golden, _scope)
    assert [g["query"] for g in out] == ["q2"]


def test_filter_reachable_project_all_keeps_everything():
    """При project='all' скоупа нет — отсеивать нечего."""
    golden = [{"query": "q", "project": "all", "expected": {"b/x.md", "c/y.md"}}]
    assert filter_reachable(golden, _scope)[0]["expected"] == {"b/x.md", "c/y.md"}


def test_filter_reachable_does_not_mutate_input():
    """Отсев возвращает новые записи: исходный набор нужен для сравнения с историей."""
    golden = [{"query": "q", "project": "a", "expected": {"a/ok.md", "b/чужая.md"}}]
    filter_reachable(golden, _scope)
    assert golden[0]["expected"] == {"a/ok.md", "b/чужая.md"}


def test_load_golden_applies_both_filters(tmp_path):
    """load_golden — единая точка сборки: разбор, отсев удалённых, отсев недостижимых."""
    kd = tmp_path / "knowledge"
    (kd / "a").mkdir(parents=True)
    (kd / "b").mkdir(parents=True)
    (kd / "a" / "ok.md").write_text("# ok", encoding="utf-8")
    (kd / "b" / "чужая.md").write_text("# чужая", encoding="utf-8")
    (kd / "_audit.log").write_text(
        json.dumps({"ts": "2026-01-01T10:00:00", "tool": "search",
                    "args": {"query": "запрос", "project": "a"}}, ensure_ascii=False) + "\n"
        + json.dumps({"ts": "2026-01-01T10:01:00", "tool": "read_article",
                      "args": {"project": "a", "filename": "ok.md"}}, ensure_ascii=False) + "\n"
        + json.dumps({"ts": "2026-01-01T10:02:00", "tool": "read_article",
                      "args": {"project": "b", "filename": "чужая.md"}}, ensure_ascii=False) + "\n"
        + json.dumps({"ts": "2026-01-01T10:03:00", "tool": "read_article",
                      "args": {"project": "a", "filename": "удалённая.md"}}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    # Режим совместимости: без предиката недостижимые остаются (историческое поведение).
    compat = load_golden(kd)
    assert compat[0]["expected"] == {"a/ok.md", "b/чужая.md"}, \
        "без in_scope отсев недостижимых включаться не должен"

    clean = load_golden(kd, _scope)
    assert clean[0]["expected"] == {"a/ok.md"}, \
        "удалённая должна выпасть по filter_existing, чужая — по filter_reachable"


# ─── Состав источников и окно по времени (v1.36.0) ───────────────────────────

def _ev(ts, tool, **args):
    return {"ts": ts, "tool": tool, "args": args}


def test_search_by_tag_is_not_a_source():
    """Тег не порождает golden-запрос: search_by_tag не ранжирует вообще.

    Он перебирает файлы, точно сверяет тег и отдаёт совпадения в порядке обхода
    каталога — «первого места» там нет, поэтому клик из такого списка ничего не
    говорит о качестве ранжирования."""
    golden = build_golden([
        _ev("2026-01-01T10:00:00", "search_by_tag", tag="docker", project="p"),
        _ev("2026-01-01T10:00:30", "read_article", project="p", filename="a.md"),
    ])
    assert golden == [], f"тег не должен становиться запросом: {golden}"


def test_tag_search_does_not_steal_reads_from_real_query():
    """Регресс, ради которого источник и убран: тег ПЕРЕНАЗНАЧАЛ клики.

    Раньше событие search_by_tag закрывало предыдущий настоящий запрос, и открытие
    статьи доставалось тегу. На живом логе из-за этого метрика была занижена: набор
    ужался всего на 3 запроса, а MRR вырос на 0.03 — открытия вернулись владельцам."""
    golden = build_golden([
        _ev("2026-01-01T10:00:00", "search", query="настоящий запрос", project="p"),
        _ev("2026-01-01T10:00:10", "search_by_tag", tag="docker", project="p"),
        _ev("2026-01-01T10:00:20", "read_article", project="p", filename="a.md"),
    ])
    assert len(golden) == 1 and golden[0]["query"] == "настоящий запрос"
    assert golden[0]["expected"] == {"p/a.md"}, \
        "открытие должно достаться поисковому запросу, а не тегу"


def test_max_gap_window_drops_late_reads():
    """Окно по времени отсекает открытия, случившиеся сильно позже запроса."""
    ev = [
        _ev("2026-01-01T10:00:00", "search", query="q", project="p"),
        _ev("2026-01-01T10:00:30", "read_article", project="p", filename="близкая.md"),
        _ev("2026-01-01T11:30:00", "read_article", project="p", filename="поздняя.md"),
    ]
    assert build_golden(ev)[0]["expected"] == {"p/близкая.md", "p/поздняя.md"}, \
        "по умолчанию окно выключено — берутся оба"
    assert build_golden(ev, max_gap_s=300)[0]["expected"] == {"p/близкая.md"}


def test_max_gap_window_off_by_default():
    """Дефолт — без окна: порог не выбран намеренно, разлома в распределении нет."""
    ev = [
        _ev("2026-01-01T10:00:00", "search", query="q", project="p"),
        _ev("2026-01-02T10:00:00", "read_article", project="p", filename="через_сутки.md"),
    ]
    assert build_golden(ev)[0]["expected"] == {"p/через_сутки.md"}


def test_max_gap_keeps_pairs_with_unparsable_timestamps():
    """Битая отметка времени не повод терять пару: окно — уточнение, а не фильтр качества."""
    ev = [
        _ev("не-дата", "search", query="q", project="p"),
        _ev("тоже-не-дата", "read_article", project="p", filename="a.md"),
    ]
    assert build_golden(ev, max_gap_s=60)[0]["expected"] == {"p/a.md"}


def test_build_golden_does_not_leak_internal_fields():
    """Служебное поле окна не должно попадать в выдачу набора."""
    golden = build_golden([
        _ev("2026-01-01T10:00:00", "search", query="q", project="p"),
        _ev("2026-01-01T10:00:05", "read_article", project="p", filename="a.md"),
    ], max_gap_s=60)
    assert "_dt" not in golden[0], f"утекло служебное поле: {golden[0].keys()}"


# ─── Сравнение конфигураций: бутстрэп + разбивка по запросам (v1.39.0) ───────

def _ranker(order):
    """Ретривер, всегда возвращающий фиксированный порядок."""
    return lambda q, p, l: order


def test_compare_identical_configs_shows_no_difference():
    golden = [{"query": f"q{i}", "project": "all", "expected": {"p/a.md"}} for i in range(20)]
    r = _ranker(["p/a.md", "p/b.md"])
    res = compare(golden, r, r, resamples=500)
    assert res["delta"] == 0.0
    assert res["better"] == 0 and res["worse"] == 0
    assert res["significant"] is False, "нулевая разница не может быть значимой"


def test_compare_detects_uniform_improvement():
    """Улучшение во ВСЕХ запросах обязано быть значимым: ДИ не накрывает ноль."""
    golden = [{"query": f"q{i}", "project": "all", "expected": {"p/target.md"}}
              for i in range(30)]
    worse = _ranker(["p/x.md", "p/y.md", "p/target.md"])   # ранг 3
    better = _ranker(["p/target.md"])                       # ранг 1
    res = compare(golden, worse, better, resamples=2000)
    assert res["delta"] > 0 and res["better"] == 30 and res["worse"] == 0
    assert res["significant"] is True
    assert res["ci_low"] > 0, f"ДИ должен быть строго положительным: {res}"


def test_compare_flags_gain_built_on_more_regressions():
    """ГЛАВНЫЙ СЛУЧАЙ, ради которого функция и написана.

    Средний MRR растёт, но запросов стало хуже БОЛЬШЕ, чем лучше: прирост держится
    на нескольких крупных улучшениях. Так выглядело «выбросить семантику» (замер
    2026-07-19): MRR 0.5212 → 0.5493 при 25 улучшениях против 27 ухудшений."""
    golden = [{"query": f"q{i}", "project": "all", "expected": {"p/t.md"}} for i in range(10)]

    def base(q, p, l):
        return ["p/x.md"] * 9 + ["p/t.md"]          # ранг 10 у всех

    def variant(q, p, l):
        # два запроса взлетают на 1-е место, шесть слегка проседают
        idx = int(q[1:])
        if idx < 2:
            return ["p/t.md"]
        if idx < 8:
            return ["p/x.md"] * 10                  # промах
        return ["p/x.md"] * 9 + ["p/t.md"]

    res = compare(golden, base, variant, resamples=1000)
    assert res["delta"] > 0, "по среднему вариант выглядит лучше"
    assert res["worse"] > res["better"], "но ухудшений больше — это и надо увидеть"
    assert "⚠️" in format_comparison(res), "вывод обязан предупреждать о такой картине"


def test_compare_is_deterministic():
    """Два прогона на одних данных дают одинаковый ДИ: иначе значимость сама шумит."""
    golden = [{"query": f"q{i}", "project": "all", "expected": {"p/t.md"}} for i in range(15)]
    a = _ranker(["p/x.md", "p/t.md"])
    b = _ranker(["p/t.md"])
    r1 = compare(golden, a, b, resamples=800)
    r2 = compare(golden, a, b, resamples=800)
    assert r1 == r2


def test_per_query_rr_marks_misses_as_zero():
    golden = [
        {"query": "q1", "project": "all", "expected": {"p/a.md"}},
        {"query": "q2", "project": "all", "expected": {"p/нет.md"}},
    ]
    assert per_query_rr(golden, _ranker(["p/a.md", "p/b.md"])) == [1.0, 0.0]


def test_compare_handles_empty_golden():
    res = compare([], _ranker([]), _ranker([]))
    assert res["n"] == 0 and res["significant"] is False
