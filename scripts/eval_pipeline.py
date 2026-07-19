"""Сетка по СТРУКТУРЕ конвейера поиска: отбор кандидатов, слияние, отсечка.

Отличие от scripts/eval_ranking.py: там крутились ВЕСА внутри уже отобранного пула
(RRF_K, decay, b-параметры) — и не двигали ничего. Диагностика (scripts/diag_retrieval.py)
показала почему: на 48.6% реальных запросов канал BM25 вообще ПУСТ (AndGroup требует все
термы), а пул кандидатов режется до скоупа проекта, поэтому цель часто не доходит до
слияния. Веса не могут переставить то, чего в пуле нет.

Здесь проверяются структурные ручки:
  * group      — AndGroup (как сейчас) / фолбэк на OrGroup при пустой выдаче / всегда Or;
  * pool       — сколько кандидатов берёт каждый канал ДО фильтра по проекту (сейчас limit*2);
  * scope      — фильтровать по проекту ВНУТРИ запроса (тогда пул набирается из проекта,
                 а не из общего топа, где чужие проекты его вытесняют);
  * threshold  — отсечка: как в проде / только относительная / без неё;
  * tiebreak   — детерминированный порядок при равных скорах.

Запросные эмбеддинги считаются ОДИН раз и переиспользуются всеми конфигурациями:
семантический канал от этих ручек не зависит, а encode — самая дорогая часть замера.

    python scripts/eval_pipeline.py            # KNOWLEDGE_DIR = копия базы

Гонять ТОЛЬКО на копии: скрипт ничего не пишет, но соседние эксперименты пересобирают
индекс, а на проде это ломает поиск.
"""
import os
import sys
import time

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from whoosh.qparser import MultifieldParser, OrGroup, AndGroup, FuzzyTermPlugin  # noqa: E402
from whoosh.query import Term  # noqa: E402
from whoosh.scoring import BM25F  # noqa: E402

from memory_compiler import search as S  # noqa: E402
from memory_compiler.config import KNOWLEDGE_DIR, decay_factor  # noqa: E402
from memory_compiler.retrieval_eval import (  # noqa: E402
    parse_audit, build_golden, filter_existing, evaluate,
)

K = str(KNOWLEDGE_DIR)
PROD_POOL_FACTOR = 2          # прод берёт limit*2 кандидатов на канал


# ─── кэш семантики: полное упорядочение по каждому запросу, считается один раз ──

_sem_cache: dict[str, list[tuple[str, float]]] = {}


def preload_semantic(queries):
    """Полный отсортированный семантический ранжир на каждый запрос.

    От структурных ручек он не зависит (меняется только то, СКОЛЬКО из него берут и
    как фильтруют), поэтому считается однократно — иначе каждая конфигурация заново
    гоняла бы encode на 140 запросов.
    """
    keys = list(S.snapshot_embeddings().keys())
    matrix = np.stack([S._embeddings[k] for k in keys])
    for i, q in enumerate(queries, 1):
        if q in _sem_cache:
            continue
        sims = matrix @ S.encode_query(q)
        best: dict[str, float] = {}
        for key, sim in zip(keys, sims):
            parent = key.split("#")[0]
            sim = float(sim)
            if parent not in best or sim > best[parent]:
                best[parent] = sim
        _sem_cache[q] = sorted(best.items(), key=lambda x: -x[1])
        if i % 40 == 0:
            print(f"  ... эмбеддинги запросов {i}/{len(queries)}", flush=True)


def _in_scope(path, project):
    return project == "all" or path.startswith(project + "/") or path in S._shared_paths


def make_retriever(*, group="and", pool=None, scope=False, threshold="prod", tiebreak=False,
                   or_scale=None):
    """Собрать функцию retrieve(query, project, limit) под заданную конфигурацию.

    or_scale — множитель OrGroup.factory(): «мягкий AND». Документ, покрывший больше
    термов запроса, получает выше скор, но частичное совпадение не выбрасывается
    (в отличие от AndGroup). None — обычный OrGroup (сумма скоров совпавших термов).
    """
    or_group = OrGroup.factory(or_scale) if or_scale is not None else OrGroup

    def retrieve(query_str, project, limit):
        ix = S.get_index()
        n_pool = pool or limit * PROD_POOL_FACTOR

        # ── канал BM25F ──────────────────────────────────────────────────
        def run(grp):
            parser = MultifieldParser(["title", "tags", "body"], schema=ix.schema, group=grp)
            parser.add_plugin(FuzzyTermPlugin())
            try:
                q = parser.parse(query_str)
            except Exception:
                return []
            # scope=True: фильтр по проекту УЧАСТВУЕТ в запросе, поэтому n_pool
            # кандидатов набирается из самого проекта, а не из общего топа.
            filt = Term("project", project) if (scope and project != "all") else None
            with ix.searcher(weighting=BM25F(title_B=S.BM25_TITLE_B, tags_B=S.BM25_TAGS_B,
                                             body_B=S.BM25_BODY_B)) as s:
                return [h["path"] for h in s.search(q, limit=n_pool, filter=filt)]

        multiword = len(query_str.split()) > 1
        if group == "or":
            bm = run(or_group)
        elif group == "and":
            bm = run(AndGroup if multiword else or_group)
        else:  # "and_then_or" — фолбэк на Or, только если And ничего не дал
            bm = run(AndGroup if multiword else or_group)
            if not bm and multiword:
                bm = run(or_group)

        # ── канал семантики ──────────────────────────────────────────────
        sem_all = _sem_cache[query_str]
        if scope:
            sem = [p for p, _ in sem_all if _in_scope(p, project)][:n_pool]
        else:
            sem = [p for p, _ in sem_all[:n_pool] if _in_scope(p, project)]

        if not scope:
            bm = [p for p in bm if _in_scope(p, project)]

        # ── слияние RRF ──────────────────────────────────────────────────
        bm_rank = {p: i + 1 for i, p in enumerate(bm)}
        sem_rank = {p: i + 1 for i, p in enumerate(sem)}
        merged = []
        for path in set(bm) | set(sem):
            rrf = 0.0
            if path in bm_rank:
                rrf += 1.0 / (S.RRF_K + bm_rank[path])
            if path in sem_rank:
                rrf += 1.0 / (S.RRF_K + sem_rank[path])
            raw = rrf * 3000 * ((1.0 - S.DECAY_WEIGHT) + S.DECAY_WEIGHT * decay_factor(path))
            merged.append((path, raw, round(raw, 1)))

        if tiebreak:
            # По сырому скору, а при точном равенстве — по пути: воспроизводимо
            # между процессами (прод сортирует округлённое и обходит set()).
            merged.sort(key=lambda x: (-x[1], x[0]))
        else:
            merged.sort(key=lambda x: -x[2])

        if not merged or S.is_low_confidence_query(query_str):
            return []
        paths = [p for p, _, _ in merged]

        if threshold == "none":
            return paths[:limit]
        top = merged[0][2]
        if threshold == "relative":
            return [p for p, _, sc in merged if sc >= top * 0.5][:limit]
        # "prod"
        if top >= 35:
            thr = max(top * 0.5, 32)
            return [p for p, _, sc in merged if sc >= thr][:limit]
        if top >= 18:
            return paths[:3]
        return []

    return retrieve


GRID = [
    ("baseline (прод)",            dict()),
    ("+ детерм. тай-брейк",        dict(tiebreak=True)),
    ("+ фолбэк And->Or",           dict(group="and_then_or")),
    ("всегда Or",                  dict(group="or")),
    ("пул 50",                     dict(pool=50)),
    ("пул 100",                    dict(pool=100)),
    ("скоуп внутри запроса",       dict(scope=True)),
    ("скоуп + пул 50",             dict(scope=True, pool=50)),
    ("скоуп + пул 100",            dict(scope=True, pool=100)),
    ("скоуп+пул50+фолбэк",         dict(scope=True, pool=50, group="and_then_or")),
    ("скоуп+пул100+фолбэк",        dict(scope=True, pool=100, group="and_then_or")),
    ("^ + относит. отсечка",       dict(scope=True, pool=100, group="and_then_or",
                                        threshold="relative")),
    ("^ + без отсечки",            dict(scope=True, pool=100, group="and_then_or",
                                        threshold="none")),
    ("^ + тай-брейк (всё вместе)", dict(scope=True, pool=100, group="and_then_or",
                                        threshold="none", tiebreak=True)),
]


def main():
    S.load_embeddings()
    golden = filter_existing(build_golden(parse_audit(K + "/_audit.log")), K)
    print(f"golden-запросов: {len(golden)} | _shared_paths: {len(S._shared_paths)}")
    print("предсчёт эмбеддингов запросов (один раз на все конфигурации)...")
    t0 = time.time()
    preload_semantic([g["query"] for g in golden])
    print(f"  готово за {time.time() - t0:.0f} с\n")

    # Контроль: конфигурация «как в проде» обязана сойтись с настоящим whoosh_search.
    def prod(q, p, l):
        return [r["project"] + "/" + r["file"] for r in S.whoosh_search(q, project=p, limit=l)]
    ref = evaluate(golden, prod, limit=10)
    mine = evaluate(golden, make_retriever(), limit=10)
    same = all(abs(ref[k] - mine[k]) < 1e-9 for k in ref)
    print(f"сверка с настоящим whoosh_search: {'СОВПАЛО' if same else 'РАСХОЖДЕНИЕ'}")
    print(f"  прод : {ref}")
    print(f"  копия: {mine}\n")
    if not same:
        print("модель конвейера расходится с продом — выводы делать нельзя")
        return

    print(f'{"конфигурация":30} {"MRR":>7} {"r@1":>7} {"r@3":>7} {"r@5":>7} {"r@10":>7}   время')
    rows = []
    for name, cfg in GRID:
        t0 = time.time()
        res = evaluate(golden, make_retriever(**cfg), limit=10)
        dt = time.time() - t0
        rows.append((name, res))
        print(f'{name:30} {res["mrr"]:>7} {res["recall@1"]:>7} {res["recall@3"]:>7} '
              f'{res["recall@5"]:>7} {res["recall@10"]:>7}   [{dt:.0f}s]')

    base = rows[0][1]
    print(f"\n=== отклонение от baseline (MRR / recall@1 / recall@10) ===")
    n = base["n"]
    for name, res in rows[1:]:
        d_mrr = res["mrr"] - base["mrr"]
        d_r1 = res["recall@1"] - base["recall@1"]
        d_r10 = res["recall@10"] - base["recall@10"]
        q1 = round(d_r1 * n)
        q10 = round(d_r10 * n)
        print(f'  {name:30} {d_mrr:+.4f} / {d_r1:+.4f} / {d_r10:+.4f}'
              f'   ({q1:+d} и {q10:+d} запросов из {n})')


if __name__ == "__main__":
    main()
