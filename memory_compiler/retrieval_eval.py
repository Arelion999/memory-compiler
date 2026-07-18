"""Оценка качества retrieval на РЕАЛЬНЫХ запросах из аудит-лога.

Зачем: измерять качество поиска было нечем — `knowledge/_bench.py` меряет только
скорость кодирования (docs/sec), а recall/MRR в проекте не считался никогда. Любое
изменение ядра (чанкование, модель эмбеддингов, пороги, реранкер) принималось на
веру, по цифрам из чужих статей. Этот модуль даёт воспроизводимое число на ЭТОМ
корпусе — коротком русском персональном, который уже дважды вёл себя не как
бенчмарочный (порог consolidate 0.9 оказался шумом; скоры related жмутся в 0.92-0.95).

Ground truth — ПОВЕДЕНЧЕСКИЙ: релевантными считаются статьи, которые реально
открыли (`read_article`) после поиска и до следующего поиска. Это аналог
click-through в поисковых системах: не мнение о релевантности, а факт того, что
искавший в итоге открыл. Синтетика (заголовок как запрос) мерила бы саму себя —
заголовки и так проиндексированы.

Ограничения, которые честно надо помнить при чтении цифр:
  * покрываются только запросы, ЗА которыми последовало открытие статьи; поиски,
    где нужное нашлось прямо в сниппете, в выборку не попадают;
  * открытие статьи не доказывает, что она лучшая — только что она оказалась
    достаточно интересной, чтобы её открыть;
  * выборка отражает историю использования, а не равномерное покрытие корпуса.
Поэтому число полезно для СРАВНЕНИЯ конфигураций между собой, а не как абсолютная
оценка «качества поиска вообще».
"""
from __future__ import annotations

import json
from pathlib import Path

# Инструменты, которые начинают новый поисковый интент: всё, что открыто ПОСЛЕ них,
# относится уже к следующему запросу.
_QUERY_TOOLS = {"search": "query", "ask": "question", "get_context": "query"}
_OPEN_TOOL = "read_article"


def parse_audit(path: str | Path) -> list[dict]:
    """Прочитать JSON-lines аудит-лог, пропуская битые строки."""
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    return entries


def _ts(entry: dict) -> str:
    return str(entry.get("ts") or "")


def build_golden(entries: list[dict], max_expected: int = 5) -> list[dict]:
    """Golden-набор из аудит-лога: [{query, project, expected: set[str]}, ...].

    Для каждого запроса собираются статьи, открытые ПОСЛЕ него и ДО следующего
    запроса (в формате "project/filename"). Запросы без открытий отбрасываются —
    для них нет ground truth. Порядок записей в логе считается хронологическим
    (сервер пишет его append-only), поэтому отдельная сортировка по ts не нужна.
    """
    golden: list[dict] = []
    pending: dict | None = None

    def flush():
        nonlocal pending
        if pending and pending["expected"]:
            golden.append(pending)
        pending = None

    for e in entries:
        tool = e.get("tool")
        args = e.get("args")
        if not isinstance(args, dict):
            args = {}
        if tool in _QUERY_TOOLS:
            flush()
            q = args.get(_QUERY_TOOLS[tool])
            if isinstance(q, str) and q.strip():
                pending = {
                    "query": q.strip(),
                    "project": (args.get("project") or "all") or "all",
                    "expected": set(),
                    "ts": _ts(e),
                }
            else:
                pending = None
        elif tool == _OPEN_TOOL and pending is not None:
            proj, fname = args.get("project"), args.get("filename")
            if isinstance(proj, str) and isinstance(fname, str) and fname:
                if len(pending["expected"]) < max_expected:
                    pending["expected"].add(f"{proj}/{fname}")
    flush()
    return golden


def filter_existing(golden: list[dict], knowledge_dir: str | Path) -> list[dict]:
    """Убрать ожидания, указывающие на удалённые статьи (иначе метрика занижается
    из-за истории, а не из-за качества поиска). Запросы без ожиданий выпадают."""
    root = Path(knowledge_dir)
    out = []
    for item in golden:
        alive = {p for p in item["expected"] if (root / p).exists()}
        if alive:
            out.append({**item, "expected": alive})
    return out


def evaluate(golden: list[dict], retrieve, ks=(1, 3, 5, 10), limit: int = 10) -> dict:
    """Прогнать retrieve по golden-набору и посчитать recall@k и MRR.

    retrieve(query, project, limit) -> список "project/filename" в порядке ранга.
    Инъекция функции позволяет сравнивать конфигурации (с реранком и без,
    разное чанкование) одним и тем же кодом и тестировать метрики без корпуса.

    recall@k здесь — доля запросов, где в топ-k попала ХОТЯ БЫ одна ожидаемая
    статья (known-item retrieval): пользователь искал конкретную вещь и нашёл её.
    MRR — среднее 1/ранг первой попавшейся ожидаемой.
    """
    hits = {k: 0 for k in ks}
    rr_sum = 0.0
    n = 0
    for item in golden:
        ranked = retrieve(item["query"], item["project"], limit) or []
        n += 1
        first_rank = None
        for idx, path in enumerate(ranked[:limit], start=1):
            if path in item["expected"]:
                first_rank = idx
                break
        if first_rank is not None:
            rr_sum += 1.0 / first_rank
            for k in ks:
                if first_rank <= k:
                    hits[k] += 1
    if not n:
        return {"n": 0, "mrr": 0.0, **{f"recall@{k}": 0.0 for k in ks}}
    return {
        "n": n,
        "mrr": round(rr_sum / n, 4),
        **{f"recall@{k}": round(hits[k] / n, 4) for k in ks},
    }
