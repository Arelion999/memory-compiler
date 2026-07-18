"""Замер качества retrieval на реальных запросах из аудит-лога.

Запуск в контейнере (там корпус, эмбеддинги и модель):
    docker exec memory-compiler-mcp python /repos/memory-compiler/scripts/eval_retrieval.py [N] [--rerank]

N — сколько последних запросов golden-набора взять (по умолчанию 60).

--rerank добавляет вторую конфигурацию (hybrid + cross-encoder). ОСТОРОЖНО: замерено
2026-07-18 на NAS — реранк стоит ~23 с на запрос (в 14 раз дороже hybrid), и этот
скрипт поднимает СВОЮ копию модели рядом с работающим сервером, в обход общего лока
embed/reranker, который в search.py стоит именно от OOM. Прогон 40 запросов с
реранком в таком режиме не дожил до конца. Поэтому по умолчанию считается только
hybrid; тяжёлую конфигурацию гонять малыми выборками или на остановленном сервере.
"""
import os
import sys
import time

sys.path.insert(0, "/app")   # контейнер
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # локальный клон

from memory_compiler.retrieval_eval import (  # noqa: E402
    parse_audit, build_golden, filter_existing, evaluate, build_known_item_set,
)

from memory_compiler.config import KNOWLEDGE_DIR  # noqa: E402

KNOWLEDGE = str(KNOWLEDGE_DIR)   # уважаем env KNOWLEDGE_DIR: эксперименты идут на КОПИИ базы
AUDIT = KNOWLEDGE + "/_audit.log"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    with_rerank = "--rerank" in sys.argv[1:]
    take = int(args[0]) if args else 60

    from memory_compiler.search import whoosh_search, load_embeddings, rerank
    from memory_compiler.handlers import SEARCH_CANDIDATE_POOL
    load_embeddings()

    entries = parse_audit(AUDIT)
    golden = filter_existing(build_golden(entries), KNOWLEDGE)
    print(f"аудит-записей: {len(entries)} | golden-запросов (после чистки): {len(golden)}")
    sample = golden[-take:]
    print(f"в выборке: {len(sample)}\n")

    def as_paths(results):
        return [r["project"] + "/" + r["file"] for r in results]

    def hybrid(query, project, limit):
        return as_paths(whoosh_search(query, project=project, limit=limit))

    def hybrid_rerank(query, project, limit):
        cand = whoosh_search(query, project=project, limit=SEARCH_CANDIDATE_POOL)
        if not cand:
            return []
        try:
            cand = rerank(query, cand, top_k=limit)
        except Exception as e:
            print("  ! rerank упал:", type(e).__name__, e)
        return as_paths(cand)

    configs = [("hybrid", hybrid)]
    if with_rerank:
        configs.append(("hybrid+rerank", hybrid_rerank))
    for name, fn in configs:
        t0 = time.time()
        res = evaluate(sample, fn, limit=10)
        dt = time.time() - t0
        line = " ".join(f"{k}={v}" for k, v in res.items())
        print(f"{name:16} {line}  [{dt:.1f}s]")

    # Known-item — ОТДЕЛЬНАЯ метрика, не смешивать с поведенческой (см. докстринг
    # build_known_item_set): она проще и самореферентна, её роль — страховочная сеть.
    if "--known-item" in sys.argv[1:]:
        ki = build_known_item_set(KNOWLEDGE)
        ki_take = int(args[1]) if len(args) > 1 else 300
        ki_sample = ki[:ki_take] if ki_take else ki
        print(f"\nknown-item набор: {len(ki)} статей (в выборке {len(ki_sample)})")
        t0 = time.time()
        res = evaluate(ki_sample, hybrid, limit=10)
        print("known-item       " + " ".join(f"{k}={v}" for k, v in res.items())
              + "  [%.1fs]" % (time.time() - t0))


if __name__ == "__main__":
    main()
