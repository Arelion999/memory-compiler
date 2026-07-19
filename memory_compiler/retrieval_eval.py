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
#
# Состав подобран ЗАМЕРОМ (2026-07-19), а не по количеству событий. Важная тонкость:
# добавление источника не только добавляет пары, но и ПЕРЕНАЗНАЧАЕТ клики — новый
# «запрос» закрывает предыдущий, и открытие статьи достаётся ему. Поэтому смотреть надо
# не на нетто-прирост, а на то, что теряется:
#   search/ask/get_context — 212 пар (база)
#   + search_by_tag        — 229 пар (−23 базовых, +40 новых). Взят: тег — это тот же
#                            retrieval-интент, и на замере вариант устойчиво лучше базы.
#   + start_task           — 241 пара (−22 базовых, +34 новых). ОТВЕРГНУТ: обменивает
#                            полноценные поисковые запросы («бэкап NAS Synology offsite
#                            хранилище», «VPS ... nginx ssh root доступ») на описания
#                            задач, часть которых как запрос бессмысленна («UserAI»,
#                            «F4+F5 двусторонний форвард ... через бота»). На замере
#                            метрика падала на всём наборе — набор становился шумнее,
#                            а не богаче.
#   search_error           — 0 (всего 5 событий)
_QUERY_TOOLS = {
    "search": "query",
    "ask": "question",
    "get_context": "query",
    "search_by_tag": "tag",
}
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


def filter_reachable(golden: list[dict], in_scope) -> list[dict]:
    """Убрать ожидания, которые поиск НЕ МОГ вернуть физически — вне скоупа запроса.

    `build_golden` считает «ответом» всё, что открыли после запроса и до следующего.
    Но запрос обычно ограничен проектом, а открыть можно что угодно: ассистент ищет
    одно, а потом лезет в статью другого проекта за кредами. Такая пара засчитывается
    как промах при ЛЮБОМ качестве поиска — измеряется недостижимое.

    Замер 2026-07-19 на живом логе: 35 пар из 299 (11.7%) указывали вне скоупа,
    а у 8 запросов из 140 (5.7%) ВСЕ ожидания были недостижимы — то есть 5.7% метрики
    были зафиксированным нулём, не зависящим от кода.

    Отсев независим от качества ранжирования: он опирается на скоуп запроса, а не на
    то, нашёл ли поиск статью. Это принципиально — иначе получился бы отбор «выкинем
    то, что не находится», и метрика росла бы сама от себя.

    in_scope(path, project) -> bool — обычно memory_compiler.search.in_search_scope
    (инъекция, чтобы модуль метрик не зависел от модуля поиска).
    """
    out = []
    for item in golden:
        alive = {p for p in item["expected"] if in_scope(p, item.get("project") or "all")}
        if alive:
            out.append({**item, "expected": alive})
    return out


def load_golden(knowledge_dir: str | Path, in_scope=None, audit_name: str = "_audit.log") -> list[dict]:
    """Стандартная сборка поведенческого набора — ЕДИНАЯ точка для всех замеров.

    Разбор лога → пары запрос/открытие → отсев удалённых статей → отсев недостижимых.
    Скрипты оценки собирали набор каждый своей строчкой, и любое уточнение метода
    (как отсев вне скоупа) приходилось разносить по пяти файлам, где оно неизбежно
    разъехалось бы: конфигурации сравнивались бы на разных наборах.

    in_scope=None (по умолчанию) отключает отсев недостижимых — режим совместимости
    для сравнения с историческими цифрами, снятыми до его появления.
    """
    root = Path(knowledge_dir)
    golden = filter_existing(build_golden(parse_audit(root / audit_name)), root)
    if in_scope is not None:
        golden = filter_reachable(golden, in_scope)
    return golden


def build_known_item_set(knowledge_dir: str | Path, skip_prefixes=("_",)) -> list[dict]:
    """Known-item набор: заголовок статьи как запрос, ожидаемый ответ — сама статья.

    ЭТО НЕ ЗАМЕНА поведенческому golden-набору, и смешивать их нельзя. Природа разная:
      * поведенческий (build_golden) — настоящие запросы и настоящие «клики», но их мало
        (133) и покрывают они лишь то, что искали в прошлом;
      * known-item — покрывает ВЕСЬ корпус, но задача заведомо проще и частично
        самореферентна: заголовок индексируется с высоким весом (title_B), так что
        высокий recall тут — не признак хорошего поиска.
    Ценность known-item в другом: это широкая СТРАХОВОЧНАЯ СЕТЬ. Она ловит катастрофу
    вида «часть корпуса перестала находиться вообще» (битый индекс, потерянные чанки,
    сломанная нарезка), которую 133 исторических запроса могут не задеть. Читать её
    как регрессионный сигнал, а не как оценку качества.

    Служебные файлы (`_log.md`, `_session.md`, ...) пропускаются: это не статьи-ответы.
    """
    root = Path(knowledge_dir)
    items: list[dict] = []
    for proj_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for md in sorted(proj_dir.glob("*.md")):
            if any(md.name.startswith(pref) for pref in skip_prefixes):
                continue
            try:
                first = md.read_text(encoding="utf-8", errors="replace").lstrip().splitlines()[:1]
            except Exception:
                continue
            if not first:
                continue
            title = first[0].lstrip("# ").strip()
            if len(title) < 8:      # слишком короткий заголовок — запрос бессмысленный
                continue
            items.append({
                "query": title,
                "project": proj_dir.name,
                "expected": {f"{proj_dir.name}/{md.name}"},
            })
    return items


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
