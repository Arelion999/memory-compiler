"""Рёбра графа: матричный расчёт обязан давать ТО ЖЕ, что попарный перебор.

Попарный цикл по всем парам статей вешал сервер на десятки секунд (v1.55.1), и
его заменили блочным матричным умножением с отбором top-K по строке. Отбор —
место, где легко потерять рёбра молча: ребро может быть слабым для одной ноды и
входить в top-K другой. Первая версия правки резала кандидатов по хвосту строки
(j > i) и теряла на плотном графе 9% итоговых рёбер — тест держит именно это.
"""
from collections import defaultdict

import numpy as np
import pytest

MAX_EDGES_PER_NODE = 8      # то же значение, что в api._build_graph
THRESHOLD = 0.45


def make_embeddings(n, d=64, clusters=8, scale=0.12, seed=7):
    """Кластеризованные единичные векторы — похоже на реальную базу: есть плотные
    группы, где у ноды кандидатов много больше, чем top-K.

    scale подобран замером: при 0.35 (норма шума в d измерениях перебивает сигнал)
    получалось 10 рёбер на 300 нод, и сравнение шло на пустом множестве — ровно то,
    что ловит test_dense_graph_actually_exercises_top_k."""
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(clusters, d)).astype(np.float32)
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    idx = rng.integers(0, clusters, n)
    m = centers[idx] + rng.normal(scale=scale, size=(n, d)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return m


def reference_edges(matrix, keys):
    """Эталон: попарный перебор — ровно то, что делал код до векторизации."""
    edges = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            sim = float(np.dot(matrix[i], matrix[j]))
            if sim > THRESHOLD:
                edges.append({"source": keys[i], "target": keys[j], "weight": round(sim, 2)})
    return edges


def blocked_edges(matrix, keys):
    """Копия расчёта из api._build_graph. Расходится с ним — тест теряет смысл,
    поэтому test_matches_production_source сверяет их текстуально."""
    edges = []
    n_emb = len(keys)
    BLOCK = 256
    seen_pairs = set()
    for start in range(0, n_emb, BLOCK):
        block = matrix[start:start + BLOCK] @ matrix.T
        for bi in range(block.shape[0]):
            i = start + bi
            row = block[bi]
            row[i] = -1.0
            hits = np.flatnonzero(row > THRESHOLD)
            if hits.size > MAX_EDGES_PER_NODE:
                # Отбор по ОКРУГЛЁННОМУ весу — тому же, что уходит в выдачу.
                # По точному отбирались чуть другие рёбра на ничьих сотых.
                weights = np.round(row[hits], 2)
                top = np.argpartition(-weights, MAX_EDGES_PER_NODE)[:MAX_EDGES_PER_NODE]
                hits = hits[top]
            for j in hits:
                j = int(j)
                pair = (i, j) if i < j else (j, i)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                edges.append({"source": keys[pair[0]], "target": keys[pair[1]],
                              "weight": round(float(row[j]), 2)})
    return edges


def apply_top_k(edges):
    """Финальный отбор из api._build_graph — сравнивать надо ИТОГОВЫЕ рёбра."""
    node_edges = defaultdict(list)
    for e in edges:
        node_edges[e["source"]].append(e)
        node_edges[e["target"]].append(e)
    kept = set()
    for _nid, lst in node_edges.items():
        lst.sort(key=lambda e: -e["weight"])
        for e in lst[:MAX_EDGES_PER_NODE]:
            kept.add((e["source"], e["target"]))
    return {(e["source"], e["target"]) for e in edges if (e["source"], e["target"]) in kept}


def strength_profile(pairs, weight_of):
    """Для каждой ноды — веса её сильнейших связей, по убыванию.

    Сравнивать НАБОРЫ ПАР побитово нельзя, и это не дефект: веса округляются до
    сотых, на плотном графе ничьих много, и какое из двух рёбер веса 0.62 попадёт
    в top-K — зависит от порядка обхода, а он у матричного расчёта свой. Замер:
    расходится ~2% пар, при этом top-K ВЕСОВ совпадает у 700 нод из 700. Проверяем
    то, что отбор обязан сохранять: у ноды остались связи той же силы.
    """
    prof = defaultdict(list)
    for a, b in pairs:
        w = weight_of[(a, b)]
        prof[a].append(w)
        prof[b].append(w)
    return {k: sorted(v, reverse=True)[:MAX_EDGES_PER_NODE] for k, v in prof.items()}


@pytest.mark.parametrize("n", [60, 300, 700])
def test_blocked_matches_pairwise(n):
    """У каждой ноды отобраны связи той же силы, что при попарном переборе."""
    matrix = make_embeddings(n)
    keys = [f"proj/a{i}.md" for i in range(n)]
    ref_edges = reference_edges(matrix.copy(), keys)
    weight_of = {(e["source"], e["target"]): e["weight"] for e in ref_edges}
    ref_pairs = apply_top_k(ref_edges)
    got_pairs = {(e["source"], e["target"]) for e in blocked_edges(matrix.copy(), keys)}
    # веса берём из эталона: так сравниваются ОДНИ И ТЕ ЖЕ числа, а не два округления
    unknown = got_pairs - set(weight_of)
    assert not unknown, f"n={n}: матричный расчёт выдал пары ниже порога: {list(unknown)[:3]}"
    ref = strength_profile(ref_pairs, weight_of)
    got = strength_profile(got_pairs, weight_of)
    assert set(got) == set(ref), (
        f"n={n}: разошёлся состав нод со связями "
        f"(эталон {len(ref)}, получено {len(got)})")
    bad = {k: (ref[k], got[k]) for k in ref if ref[k] != got[k]}
    assert not bad, (
        f"n={n}: у {len(bad)} нод из {len(ref)} связи другой силы, например "
        f"{list(bad.items())[:2]}")
    # объём выдачи не должен уезжать: ничьи меняют состав, но не количество
    assert abs(len(got_pairs) - len(ref_pairs)) <= max(5, len(ref_pairs) * 0.03), (
        f"n={n}: рёбер {len(got_pairs)} против {len(ref_pairs)} — расхождение больше ничьих")


def test_dense_graph_actually_exercises_top_k():
    """Позитивный контроль: без него совпадение пустых множеств проходило бы всегда.

    Первая (неверная) версия правки резала кандидатов по хвосту строки и падала
    именно на нодах, у которых кандидатов больше top-K — тест обязан такие иметь.
    """
    n = 300
    matrix = make_embeddings(n)
    keys = [f"proj/a{i}.md" for i in range(n)]
    edges = reference_edges(matrix.copy(), keys)
    assert len(edges) > 500, f"граф слишком редкий для проверки: {len(edges)} рёбер"
    degree = defaultdict(int)
    for e in edges:
        degree[e["source"]] += 1
        degree[e["target"]] += 1
    crowded = sum(1 for v in degree.values() if v > MAX_EDGES_PER_NODE)
    assert crowded > n // 3, f"нод с кандидатами больше top-K всего {crowded}"


def test_matches_production_source():
    """Копия в тесте не разошлась с рабочим кодом api._build_graph.

    Проверяем по опорным строкам: тест сравнивает СВОЮ реализацию с эталоном, и
    если рабочая уедет отдельно, проверка станет декоративной.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "memory_compiler" / "api.py").read_text(
        encoding="utf-8")
    for anchor in (
        "block = matrix[start:start + BLOCK] @ matrix.T",
        "hits = np.flatnonzero(row > 0.45)",
        "np.argpartition(-weights, MAX_EDGES_PER_NODE)[:MAX_EDGES_PER_NODE]",
        "pair = (i, j) if i < j else (j, i)",
        "row[i] = -1.0",
    ):
        assert anchor in src, f"в api.py нет опорной строки расчёта рёбер: {anchor!r}"
