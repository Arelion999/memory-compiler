"""Эксперимент по нарезке: пересобрать индекс+эмбеддинги под текущей политикой и замерить.

Гоняется ТОЛЬКО на копии базы (KNOWLEDGE_DIR указывает на копию), потому что
пересобирает .embeddings.pkl и .whoosh_index с нуля — на проде это стёрло бы рабочий кэш.

    docker run ... -e KNOWLEDGE_DIR=/kexp -e CHUNK_ADAPTIVE=1 -e CHUNK_SUBCHUNKS_CAP=16 \\
        ... python /repos/memory-compiler/scripts/eval_chunking.py [N]

Печатает конфигурацию, число чанков, время пересборки и метрики (recall@k, MRR),
чтобы конфигурации сравнивались между собой одним и тем же кодом.
"""
import os
import sys
import time

# Работает и в контейнере (/app), и при локальном запуске из клона репозитория —
# эксперимент по нарезке считается на копии базы, ему прод-окружение не нужно,
# а на обычном CPU он идёт в разы быстрее, чем на NAS.
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_compiler import search as S  # noqa: E402
from memory_compiler.config import KNOWLEDGE_DIR  # noqa: E402
from memory_compiler.retrieval_eval import (  # noqa: E402
    parse_audit, build_golden, filter_existing, evaluate,
)

K = str(KNOWLEDGE_DIR)


def main():
    take = int(sys.argv[1]) if len(sys.argv) > 1 else 0   # 0 = весь golden-набор

    print("KNOWLEDGE_DIR = %s" % K)
    print("политика: adaptive=%s body_max=%d max_subchunks=%d window_max=%d subchunks_cap=%d"
          % (S.CHUNK_ADAPTIVE, S.CHUNK_BODY_MAX, S.CHUNK_MAX_SUBCHUNKS,
             S.CHUNK_WINDOW_MAX, S.CHUNK_SUBCHUNKS_CAP))

    t0 = time.time()
    n_idx = S.rebuild_index()
    t_idx = time.time() - t0
    t0 = time.time()
    S.rebuild_embeddings()
    t_emb = time.time() - t0
    n_chunks = len(S.snapshot_embeddings())
    print("индекс: %s статей за %.1f с | эмбеддинги: %d чанков за %.1f с"
          % (n_idx, t_idx, n_chunks, t_emb))

    golden = filter_existing(build_golden(parse_audit(K + "/_audit.log")), K)
    sample = golden[-take:] if take else golden
    print("golden: %d (в выборке %d)" % (len(golden), len(sample)))

    def hybrid(query, project, limit):
        return [r["project"] + "/" + r["file"]
                for r in S.whoosh_search(query, project=project, limit=limit)]

    t0 = time.time()
    res = evaluate(sample, hybrid, limit=10)
    print("РЕЗУЛЬТАТ  " + " ".join(f"{k}={v}" for k, v in res.items())
          + "  [%.1fs]" % (time.time() - t0))


if __name__ == "__main__":
    main()
