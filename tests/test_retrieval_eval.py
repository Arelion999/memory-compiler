"""Тесты харнесса оценки retrieval (v1.26.0).

Метрика — инструмент принятия решений по ядру поиска, поэтому сама она должна быть
проверена: ошибка в подсчёте recall/MRR или в сборке golden-набора привела бы к
неверному выводу «стало лучше/хуже» и к изменению чанкования на ложных основаниях.
"""
from memory_compiler.retrieval_eval import build_golden, evaluate, filter_existing


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
