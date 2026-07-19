"""Диагностика: ГДЕ именно теряется целевая статья на пути запроса.

Сетки по параметрам (v1.30.0) не двигали MRR, потому что крутили ранжирование
ВНУТРИ пула кандидатов, не проверив, доходит ли цель до пула вообще. Этот скрипт
не меняет ничего — он разбирает конвейер на этапы и говорит, на каком именно
теряется ожидаемая статья:

  1. нет даже в широком пуле (WIDE по каждому каналу) — проблема представления
     (индекс/чанки/эмбеддинги), ранжированием не лечится;
  2. есть в широком, но не в узком (limit*2) — добор кандидатов мал;
  3. была в пуле, но выпала на скоупе проекта — скоуп применяется ПОСЛЕ выборки;
  4. дошла до слияния, но срезана порогом отсечки;
  5. в выдаче, но не первой — вот это и есть работа для ранжирования.

Размер группы (2) + (3) — это потолок, который может дать починка выборки
кандидатов. Размер (5) — потолок, который может дать тюнинг весов/бустов.

Ничего не пишет в базу: только читает индекс и .embeddings.pkl. Безопасен на проде.

    docker exec memory-compiler-mcp python /repos/memory-compiler/scripts/diag_retrieval.py
    python scripts/diag_retrieval.py            # локально, KNOWLEDGE_DIR=копия

Полный по-запросный разбор пишется в <KNOWLEDGE_DIR>/../diag_retrieval.jsonl
(в консоль — только сводка: консоль на Windows теряет кириллицу при фильтрации).
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whoosh.qparser import MultifieldParser, OrGroup, AndGroup, FuzzyTermPlugin  # noqa: E402
from whoosh.scoring import BM25F  # noqa: E402

from memory_compiler import search as S  # noqa: E402
from memory_compiler.config import KNOWLEDGE_DIR, decay_factor  # noqa: E402
from memory_compiler.retrieval_eval import (  # noqa: E402
    parse_audit, build_golden, filter_existing,
)

K = str(KNOWLEDGE_DIR)
WIDE = 200          # «широкий» пул: сколько кандидатов канал МОГ БЫ отдать
LIMIT = 10          # как в замерах (eval_ranking / eval_chunking)


def _rank_of(paths, expected) -> int | None:
    """1-based ранг первой ожидаемой статьи в списке путей, либо None."""
    for i, p in enumerate(paths, start=1):
        if p in expected:
            return i
    return None


def _scoped(paths, project):
    """Отфильтровать по проекту ТАК ЖЕ, как это делает whoosh_search."""
    if project == "all":
        return list(paths)
    return [p for p in paths
            if p.startswith(project + "/") or p in S._shared_paths]


def diag_one(ix, item):
    query_str = item["query"]
    project = item["project"]
    expected = item["expected"]

    # ── канал BM25F ──────────────────────────────────────────────────────
    group = AndGroup if len(query_str.split()) > 1 else OrGroup
    parser = MultifieldParser(["title", "tags", "body"], schema=ix.schema, group=group)
    parser.add_plugin(FuzzyTermPlugin())
    try:
        q = parser.parse(query_str)
    except Exception:
        q = None

    bm_wide = []
    if q is not None:
        with ix.searcher(weighting=BM25F(title_B=S.BM25_TITLE_B, tags_B=S.BM25_TAGS_B,
                                         body_B=S.BM25_BODY_B)) as s:
            bm_wide = [h["path"] for h in s.search(q, limit=WIDE)]

    # Тот же запрос через OrGroup — оценить, сколько отнимает требование «все термы».
    parser_or = MultifieldParser(["title", "tags", "body"], schema=ix.schema, group=OrGroup)
    parser_or.add_plugin(FuzzyTermPlugin())
    bm_or_wide = []
    try:
        q_or = parser_or.parse(query_str)
        with ix.searcher(weighting=BM25F(title_B=S.BM25_TITLE_B, tags_B=S.BM25_TAGS_B,
                                         body_B=S.BM25_BODY_B)) as s:
            bm_or_wide = [h["path"] for h in s.search(q_or, limit=WIDE)]
    except Exception:
        pass

    # ── канал семантики ──────────────────────────────────────────────────
    # top-N отсортирован, поэтому узкий пул — префикс широкого (один вызов на оба).
    try:
        sem_wide = [p for p, _ in S.semantic_search(query_str, limit=WIDE)]
    except Exception:
        sem_wide = []

    # ── как это видит прод: узкий пул (limit*2), затем скоуп ─────────────
    bm_narrow = bm_wide[:LIMIT * 2]
    sem_narrow = sem_wide[:LIMIT * 2]
    bm_final = _scoped(bm_narrow, project)
    sem_final = _scoped(sem_narrow, project)

    # ── слияние RRF (повторяет whoosh_search) ────────────────────────────
    bm_rank = {p: i + 1 for i, p in enumerate(bm_final)}
    sem_rank = {p: i + 1 for i, p in enumerate(sem_final)}
    merged = []
    for path in set(bm_final) | set(sem_final):
        rrf = 0.0
        if path in bm_rank:
            rrf += 1.0 / (S.RRF_K + bm_rank[path])
        if path in sem_rank:
            rrf += 1.0 / (S.RRF_K + sem_rank[path])
        score = round(rrf * 3000 * ((1.0 - S.DECAY_WEIGHT)
                                    + S.DECAY_WEIGHT * decay_factor(path)), 1)
        merged.append((path, score))
    merged.sort(key=lambda x: -x[1])
    merged_paths = [p for p, _ in merged]

    # отсечка, как в whoosh_search
    after_cut = []
    if merged:
        top = merged[0][1]
        if top >= 35:
            thr = max(top * 0.5, 32)
            after_cut = [p for p, sc in merged if sc >= thr][:LIMIT]
        elif top >= 18:
            after_cut = merged_paths[:3]      # мягкий путь, приближённо

    # ── настоящая выдача прода (эталон) ──────────────────────────────────
    real = [r["project"] + "/" + r["file"]
            for r in S.whoosh_search(query_str, project=project, limit=LIMIT)]

    r_bm_wide = _rank_of(bm_wide, expected)
    r_sem_wide = _rank_of(sem_wide, expected)
    r_bm_narrow = _rank_of(bm_narrow, expected)
    r_sem_narrow = _rank_of(sem_narrow, expected)
    r_bm_final = _rank_of(bm_final, expected)
    r_sem_final = _rank_of(sem_final, expected)
    r_merged = _rank_of(merged_paths, expected)
    r_real = _rank_of(real, expected)

    # ── атрибуция потери ─────────────────────────────────────────────────
    if r_real == 1:
        cat = "1_первой"
    elif r_real is not None:
        cat = "2_в_выдаче_не_первой"
    elif r_merged is not None:
        cat = "3_срезана_порогом"
    elif r_bm_final is None and r_sem_final is None and (
            r_bm_narrow is not None or r_sem_narrow is not None):
        cat = "4_выпала_на_скоупе"
    elif r_bm_wide is not None or r_sem_wide is not None:
        cat = "5_есть_в_широком_нет_в_узком"
    else:
        cat = "6_нет_даже_в_широком"

    return {
        "query": query_str,
        "project": project,
        "words": len(query_str.split()),
        "scoped": project != "all",
        "expected": sorted(expected),
        "bm25_and_hits": len(bm_wide),
        "bm25_or_hits": len(bm_or_wide),
        "bm25_and_empty": len(bm_wide) == 0,
        "pool_bm_narrow": len(bm_narrow),
        "pool_bm_after_scope": len(bm_final),
        "pool_sem_narrow": len(sem_narrow),
        "pool_sem_after_scope": len(sem_final),
        "pool_merged": len(merged_paths),
        "returned": len(real),
        "rank_bm_wide": r_bm_wide,
        "rank_sem_wide": r_sem_wide,
        "rank_bm_after_scope": r_bm_final,
        "rank_sem_after_scope": r_sem_final,
        "rank_merged": r_merged,
        "rank_real": r_real,
        "category": cat,
        "repro_ok": after_cut[:LIMIT] == real,
    }


def main():
    take = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    S.load_embeddings()
    ix = S.get_index()
    with ix.searcher() as s:
        n_docs = s.doc_count()
    print(f"KNOWLEDGE_DIR = {K}")
    print(f"документов в индексе: {n_docs} | чанков-эмбеддингов: {len(S.snapshot_embeddings())}")
    print(f"_shared_paths: {len(S._shared_paths)} "
          f"({'ПУСТ — кросс-проектные статьи не работают' if not S._shared_paths else 'заполнен'})")

    golden = filter_existing(build_golden(parse_audit(K + "/_audit.log")), K)
    sample = golden[-take:] if take else golden
    print(f"golden-запросов: {len(golden)} (в выборке {len(sample)})\n")

    t0 = time.time()
    rows = []
    for i, item in enumerate(sample, 1):
        rows.append(diag_one(ix, item))
        if i % 25 == 0:
            print(f"  ... {i}/{len(sample)}", flush=True)
    dt = time.time() - t0

    out_path = Path(K).parent / "diag_retrieval.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(rows)
    scoped = [r for r in rows if r["scoped"]]
    print(f"\n{'=' * 70}\nСВОДКА (n={n}, {dt:.0f}s)\n{'=' * 70}")
    print(f"  запросов со скоупом на проект: {len(scoped)} ({100 * len(scoped) / n:.0f}%)")
    print(f"  воспроизведение выдачи прода:  {sum(r['repro_ok'] for r in rows)}/{n}"
          f"  (расхождения = недетерминированный тай-брейк)")

    print(f"\n  ── AndGroup: требование «все термы» ──")
    empty = [r for r in rows if r["bm25_and_empty"]]
    print(f"  запросов с ПУСТЫМ каналом BM25: {len(empty)} ({100 * len(empty) / n:.0f}%)")
    rescued = [r for r in empty if r["bm25_or_hits"] > 0]
    print(f"  из них OrGroup нашёл бы хоть что-то: {len(rescued)}")
    by_words: dict[int, list] = {}
    for r in rows:
        by_words.setdefault(min(r["words"], 6), []).append(r)
    for w in sorted(by_words):
        g = by_words[w]
        e = sum(x["bm25_and_empty"] for x in g)
        print(f"    {w}{'+' if w == 6 else ' '} слов: {len(g):>3} запр., пустой BM25 у {e:>3}"
              f" ({100 * e / len(g):>3.0f}%)")

    print(f"\n  ── размер пула кандидатов ──")
    def avg(key, subset):
        return sum(r[key] for r in subset) / len(subset) if subset else 0
    for label, subset in (("все", rows), ("scoped", scoped)):
        if not subset:
            continue
        print(f"  {label:>7}: BM25 {avg('pool_bm_narrow', subset):5.1f} -> "
              f"{avg('pool_bm_after_scope', subset):5.1f} после скоупа | "
              f"семантика {avg('pool_sem_narrow', subset):5.1f} -> "
              f"{avg('pool_sem_after_scope', subset):5.1f} | "
              f"слияние {avg('pool_merged', subset):5.1f} | "
              f"отдано {avg('returned', subset):4.1f}")

    print(f"\n  ── ГДЕ ТЕРЯЕТСЯ ЦЕЛЬ ──")
    cats: dict[str, int] = {}
    for r in rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    for cat in sorted(cats):
        print(f"  {cat:<30} {cats[cat]:>4}  ({100 * cats[cat] / n:>4.1f}%)")

    headroom = cats.get("4_выпала_на_скоупе", 0) + cats.get("5_есть_в_широком_нет_в_узком", 0)
    ranking = cats.get("2_в_выдаче_не_первой", 0)
    cut = cats.get("3_срезана_порогом", 0)
    print(f"\n  потолок починки ВЫБОРКИ кандидатов (4+5): {headroom} запр. "
          f"({100 * headroom / n:.1f} п.п. recall)")
    print(f"  потолок починки ОТСЕЧКИ (3):              {cut} запр. "
          f"({100 * cut / n:.1f} п.п. recall)")
    print(f"  потолок тюнинга РАНЖИРОВАНИЯ (2):         {ranking} запр. "
          f"(это всё, на что могут влиять бусты полей)")
    print(f"  недостижимо без работы с представлением:  {cats.get('6_нет_даже_в_широком', 0)} запр.")
    print(f"\nпо-запросный разбор: {out_path}")


if __name__ == "__main__":
    main()
