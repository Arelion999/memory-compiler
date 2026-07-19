"""Сетка по параметрам ранжирования: что даёт RRF_K, доля decay и b-параметры BM25F.

Все три применяются на этапе ЗАПРОСА, поэтому конфигурации сравниваются в ОДНОМ
процессе — без пересборки индекса, ре-эмбеддинга и рестартов. Один прогон сетки ≈
минута на конфигурацию.

    docker exec memory-compiler-mcp python /repos/memory-compiler/scripts/eval_ranking.py

Читает индекс и эмбеддинги как есть (ничего не пишет), поэтому безопасен на проде.
Целевая метрика здесь — MRR и recall@1: recall@10 упирается в то, НАЙДЕНА ли статья
вообще, а ранжирование отвечает за то, на каком она месте.
"""
import os
import sys
import time

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_compiler import search as S  # noqa: E402
from memory_compiler.config import KNOWLEDGE_DIR  # noqa: E402
from memory_compiler.retrieval_eval import (  # noqa: E402
    load_golden, evaluate,
)

K = str(KNOWLEDGE_DIR)

# Сетка: по одной ручке за раз, чтобы эффект был атрибутируем. Комбинации — только
# после того, как станет видно, какие одиночные сдвиги вообще что-то дают.
GRID = [
    ("baseline (как в проде)", {}),
    ("RRF_K=10",              {"RRF_K": 10}),
    ("RRF_K=20",              {"RRF_K": 20}),
    ("RRF_K=120",             {"RRF_K": 120}),
    ("decay=0 (без свежести)", {"DECAY_WEIGHT": 0.0}),
    ("decay=0.6",             {"DECAY_WEIGHT": 0.6}),
    ("BM25 b=0.3",            {"BM25_TITLE_B": 0.3, "BM25_TAGS_B": 0.3, "BM25_BODY_B": 0.3}),
    ("BM25 b=1.0",            {"BM25_TITLE_B": 1.0, "BM25_TAGS_B": 1.0, "BM25_BODY_B": 1.0}),
]


def main():
    S.load_embeddings()
    print("эмбеддингов:", len(S.snapshot_embeddings()))

    golden = load_golden(K, S.in_search_scope)
    print("golden-запросов:", len(golden), "\n")

    def hybrid(query, project, limit):
        return [r["project"] + "/" + r["file"]
                for r in S.whoosh_search(query, project=project, limit=limit)]

    print(f'{"конфигурация":26} {"MRR":>7} {"r@1":>7} {"r@3":>7} {"r@5":>7} {"r@10":>7}   время')
    rows = []
    for name, over in GRID:
        saved = {k: getattr(S, k) for k in over}
        for k, v in over.items():
            setattr(S, k, v)
        try:
            t0 = time.time()
            res = evaluate(golden, hybrid, limit=10)
            dt = time.time() - t0
        finally:
            for k, v in saved.items():
                setattr(S, k, v)
        rows.append((name, res))
        print(f'{name:26} {res["mrr"]:>7} {res["recall@1"]:>7} {res["recall@3"]:>7} '
              f'{res["recall@5"]:>7} {res["recall@10"]:>7}   [{dt:.0f}s]')

    base = rows[0][1]
    print("\n=== отклонение от baseline (MRR / recall@1) ===")
    for name, res in rows[1:]:
        d_mrr = res["mrr"] - base["mrr"]
        d_r1 = res["recall@1"] - base["recall@1"]
        mark = "лучше" if (d_mrr > 0 and d_r1 >= 0) else ("хуже" if d_mrr < 0 else "спорно")
        print(f'  {name:26} {d_mrr:+.4f} / {d_r1:+.4f}   {mark}')


if __name__ == "__main__":
    main()
