"""Сборка индекса и эмбеддингов на КОПИИ базы — подготовка к замеру baseline.

Отделено от scripts/eval_run.py намеренно: сборка идёт десятки минут (полный encode
корпуса), сам замер — секунды. Разделение позволяет прогнать все методики по ОДНОМУ
индексу, не пересобирая его под каждую, и переснять метрики после правки харнесса
без повторного encode.

    KNOWLEDGE_DIR=/путь/к/копии python scripts/eval_build.py

⚠️ ТОЛЬКО НА КОПИИ. Скрипт ПЕРЕСОБИРАЕТ .whoosh_index и .embeddings.pkl; на боевой
базе поиск ломается до конца пересборки. Два известных боевых пути отклоняются:
`/knowledge` (монтирование в контейнере) и `knowledge/` внутри клона — второй опаснее,
чем кажется, потому что каталог живьём синхронится на NAS и индекс замера уехал бы
на прод. Обход — `--force`, осознанно.

⚠️ КОРПУС БРАТЬ С ПРОДА, А НЕ ИЗ ЛОКАЛЬНОГО ЗЕРКАЛА. Проверено 2026-07-30: зеркало
разошлось с боевой базой (лишние файлы, отставший `_audit.log`), а `_audit.log` — это
источник golden-набора, то есть расхождение искажает сам предмет замера.

Конфиг читается из env, как в проде: чтобы цифры были сопоставимы с записанным в
CLAUDE.md baseline, env обязан совпадать с боевым (docker-compose.yml + .env) —
в частности EMBED_MODEL и LATE_CHUNKING. Скрипт печатает то, с чем реально собрал,
именно для того, чтобы это можно было сверить постфактум.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")   # контейнер
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # клон


def refuse_reason(target: Path) -> str | None:
    """Причина отказа, если target — боевая база. None — можно работать.

    Дублируется в eval_run.py сознательно: раннеры самодостаточны, каталог scripts/
    не пакет, а десять строк проверки дешевле импортной гимнастики.
    """
    raw = os.environ.get("KNOWLEDGE_DIR", "")
    if not raw:
        return "KNOWLEDGE_DIR не задан — по умолчанию это /knowledge, боевая база"
    if raw.replace("\\", "/").rstrip("/") == "/knowledge":
        return "/knowledge — боевая база в контейнере"
    repo_kb = Path(__file__).resolve().parent.parent / "knowledge"
    if repo_kb.exists() and target == repo_kb.resolve():
        return (f"{repo_kb} — рабочая копия репозитория; она синхронится на NAS, "
                "индекс замера уехал бы на прод")
    return None


def main() -> int:
    from memory_compiler.config import KNOWLEDGE_DIR
    from memory_compiler import search as S

    target = Path(KNOWLEDGE_DIR).resolve()
    reason = refuse_reason(target)
    if reason and "--force" not in sys.argv:
        print(f"ОТКАЗ: {reason}.\n"
              f"Задай KNOWLEDGE_DIR на копию базы либо добавь --force, если уверен.",
              file=sys.stderr)
        return 2

    print("корпус     :", KNOWLEDGE_DIR, flush=True)
    print("модель     :", S.EMBED_MODEL_NAME, flush=True)
    print("late_chunk :", S.LATE_CHUNKING, " adaptive:", S.CHUNK_ADAPTIVE,
          " ctx_ver:", S.CONTEXT_FORMAT_VERSION, flush=True)
    print("поиск      : group=%s pool=%s scope_aware=%s"
          % (S.SEARCH_QUERY_GROUP, S.SEARCH_POOL, S.SEARCH_SCOPE_AWARE), flush=True)

    t0 = time.time()
    n_docs = S.rebuild_index()
    t1 = time.time()
    print(f"\nWhoosh: {n_docs} документов  [{t1 - t0:.0f} c]", flush=True)

    n_chunks = S.rebuild_embeddings()
    t2 = time.time()
    print(f"Эмбеддинги: {n_chunks} чанков  [{t2 - t1:.0f} c]", flush=True)
    print(f"ИТОГО сборка: {t2 - t0:.0f} c", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
