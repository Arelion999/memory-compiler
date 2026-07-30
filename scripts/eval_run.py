"""Замер baseline на КОПИИ базы: обе поведенческие методики + known-item.

    KNOWLEDGE_DIR=/путь/к/копии python scripts/eval_build.py   # сперва собрать
    KNOWLEDGE_DIR=/путь/к/копии python scripts/eval_run.py     # затем замерить

Аргумент — какие наборы считать: `all` (по умолчанию), `behavioral`, `known-item`.
Поведенческие идут секунды, known-item — минуты, отсюда возможность разделить.

ПОЧЕМУ ОБЕ ПОВЕДЕНЧЕСКИЕ СРАЗУ. Канон — `load_golden(+in_search_scope)`, он отсеивает
недостижимые ожидания (запрос ограничен проектом, а открыть после него могли что
угодно). Историческая — `filter_existing` без этого отсева, как в eval_retrieval.py.
Однажды уже случилось, что по записям было НЕ восстановить, какой из двух методик
снят прежний baseline, и цифры оказались несравнимы. Снимаем обе всегда.

KNOWN-ITEM НЕ СМЕШИВАТЬ с поведенческим: заголовок статьи как запрос — задача заведомо
проще и частично самореферентна (заголовок индексируется с высоким весом). Его роль —
широкая страховочная сеть на «часть корпуса перестала находиться вообще», а не оценка
качества. Читать как регрессионный сигнал.

⚠️ ЧИТАТЬ ЦИФРЫ ОСТОРОЖНО. Рост baseline сам по себе НЕ означает, что поиск стал лучше:
в этом проекте четыре замера подряд росли, и ни один рост не был вызван улучшением
ранжирования — двигались измерение, набор или корпус. Сравнивать можно только при
одинаковой версии харнесса И одинаковом источнике набора; при разборе смотреть не на
дельту MRR, а на то, как прирост распределился по k (сдвиг только в хвосте топ-10 при
неподвижном r@1 — признак смены состава, а не ранжирования).
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")   # контейнер
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # клон


def refuse_reason(target: Path) -> str | None:
    """Причина отказа, если target — боевая база. None — можно работать.

    Дублируется в eval_build.py сознательно (см. комментарий там). Здесь скрипт
    только читает, но замер по живой базе всё равно бессмыслен: корпус меняется
    под ногами, а результат нечем воспроизвести.
    """
    raw = os.environ.get("KNOWLEDGE_DIR", "")
    if not raw:
        return "KNOWLEDGE_DIR не задан — по умолчанию это /knowledge, боевая база"
    if raw.replace("\\", "/").rstrip("/") == "/knowledge":
        return "/knowledge — боевая база в контейнере"
    repo_kb = Path(__file__).resolve().parent.parent / "knowledge"
    if repo_kb.exists() and target == repo_kb.resolve():
        return f"{repo_kb} — рабочая копия репозитория, она синхронится на NAS"
    return None


def main() -> int:
    from memory_compiler.config import KNOWLEDGE_DIR
    from memory_compiler import search as S
    from memory_compiler.search import whoosh_search, in_search_scope
    from memory_compiler.retrieval_eval import (
        parse_audit, load_golden, evaluate, build_known_item_set,
    )

    target = Path(KNOWLEDGE_DIR).resolve()
    reason = refuse_reason(target)
    if reason and "--force" not in sys.argv:
        print(f"ОТКАЗ: {reason}.\n"
              f"Задай KNOWLEDGE_DIR на копию базы либо добавь --force, если уверен.",
              file=sys.stderr)
        return 2

    which = next((a for a in sys.argv[1:] if not a.startswith("--")), "all")
    K = str(KNOWLEDGE_DIR)
    S.load_embeddings()

    def retrieve(query, project, limit):
        """Боевой путь поиска — ровно то, что зовёт хендлер search."""
        return [r["project"] + "/" + r["file"]
                for r in whoosh_search(query, project=project, limit=limit)]

    def show(label, golden, ks=(1, 3, 5, 10)):
        t0 = time.time()
        res = evaluate(golden, retrieve, ks=ks, limit=10)
        parts = [f"n={res['n']}", f"MRR {res['mrr']:.4f}"]
        parts += [f"r@{k} {res[f'recall@{k}']:.4f}" for k in ks]
        print(f"{label:52} " + "  ".join(parts) + f"   [{time.time() - t0:.0f} c]",
              flush=True)

    print(f"корпус: {K}", flush=True)
    print(f"аудит-записей: {len(parse_audit(os.path.join(K, '_audit.log')))} | "
          f"*.md: {sum(1 for _ in Path(K).rglob('*.md'))} | "
          f"эмбеддингов: {len(S._embeddings)}\n", flush=True)

    if which in ("all", "behavioral"):
        show("поведенческий, КАНОН load_golden(+in_search_scope)",
             load_golden(K, in_scope=in_search_scope))
        show("поведенческий, историческая (filter_existing)",
             load_golden(K))          # in_scope=None — режим совместимости
    if which in ("all", "known-item"):
        show("known-item (страховочная сеть)", build_known_item_set(K), ks=(1, 5))
    return 0


if __name__ == "__main__":
    sys.exit(main())
