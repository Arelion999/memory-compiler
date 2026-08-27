"""Суточный замер качества памяти, который делает себя сам (v1.73.0).

Контрольный замер после релизов v1.65–v1.72 не был снят ни разу: он держался на
том, что о нём вспомнят. В этом же проекте есть готовый прецедент — `stale_facts`
и `knowledge_gap` за 4.5 месяца не позвали ни разу, и именно поэтому напоминание
о `session_note` делает сервер, а не человек. Здесь то же решение: считает
сервер по расписанию и сам кладёт результат в базу.

Запуск: `POST /api/metrics/daily` у РАБОТАЮЩЕГО сервера
(`scripts/mc-daily-metrics.sh` — обёртка для cron на NAS). Ручной прогон
`python -m memory_compiler.daily_metrics` оставлен для отладки вне сервера.

⚠️ ЗАПИСЬ ИДЁТ ЧЕРЕЗ `save_lesson`, а не прямой записью файла. Хендлер сам
находит статью по теме, дописывает запись, индексирует и коммитит; прямая запись
дала бы статью, которой нет ни в индексе, ни в git — то есть замер, который не
находится поиском.

⚠️ СНИМОК В КОНЦЕ ЗАПИСИ (`MTR {...}`) — не украшение: следующий прогон читает
его, чтобы показать СДВИГ. Ряд без предыдущей точки нечитаем — «медиана 6800»
ничего не значит, пока рядом не стоит «было 14287».
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

SNAPSHOT_MARK = "MTR "
_SNAP_RE = re.compile(r"^MTR (\{.*\})\s*$", re.MULTILINE)

TOPIC = "Суточный замер качества памяти"
PROJECT = "memory-compiler"
TAGS = ["метрики", "замер", "аналитика", "суточный-ряд"]


def parse_snapshot(text: str) -> dict | None:
    """Последний машиночитаемый снимок из текста статьи.

    ⚠️ Берётся ПОСЛЕДНИЙ: записи копятся сверху вниз, и сравнивать надо с
    ближайшей по времени, а не с первой попавшейся. Порченая строка гасится —
    замер важнее дельты, и терять сегодняшние цифры из-за вчерашней опечатки
    нельзя.
    """
    found = _SNAP_RE.findall(text or "")
    for raw in reversed(found):
        try:
            return json.loads(raw)
        except Exception:
            continue
    return None


def _shift(name: str, now, was, fmt=lambda v: str(v)) -> str:
    if was is None or was == now:
        return "%s %s" % (name, fmt(now))
    return "%s %s → %s" % (name, fmt(was), fmt(now))


def _pct(v) -> str:
    return "%d%%" % round(float(v) * 100)


def format_report(d: dict, prev: dict | None) -> str:
    """Человекочитаемый отчёт плюс снимок для следующего прогона."""
    p = prev or {}
    lines = [
        "Замер за последние %g ч. Вызовов %d, сессий %d." % (
            d["hours"], d["calls"], d["sessions"]),
        "",
        "ПОИСК. " + ", ".join([
            _shift("запросов", d["searches"], p.get("searches")),
            _shift("медиана выдачи", d["search_median"], p.get("search_median")),
            _shift("доля символов", d["search_share"], p.get("search_share"), _pct),
            _shift("впустую", d["miss_rate"], p.get("miss_rate"), _pct),
            _shift("привело к действию", d["act_rate"], p.get("act_rate"), _pct),
        ]) + ".",
        "КОНТЕКСТ СЕССИИ. " + ", ".join([
            _shift("контекст загружен где-либо", d.get("ctx_rate", 0),
                   p.get("ctx_rate"), _pct),
            _shift("стартуют вслепую", d["blind_rate"], p.get("blind_rate"), _pct),
            "%d из %d" % (d["blind"], d["sessions"]),
            _shift("заметок по ходу", d["notes"], p.get("notes")),
            _shift("finish_task без сводки", d["no_summary_rate"],
                   p.get("no_summary_rate"), _pct),
            "%d из %d" % (d["finish_no_summary"], d["finish_total"]),
        ]) + ".",
        "",
        "Нарезка сессий по паузе %d мин — эвристика (аудит не пишет session_id), "
        "цифра слепых стартов от порога зависит. Ряд сравним только сам с собой."
        % (_gap_minutes(),),
        "",
        SNAPSHOT_MARK + json.dumps(d, ensure_ascii=False, sort_keys=True),
    ]
    return "\n".join(lines)


def _gap_minutes() -> int:
    from memory_compiler.analytics import SESSION_GAP_SEC
    return int(SESSION_GAP_SEC // 60)


def _previous_snapshot() -> dict | None:
    """Снимок прошлого прогона — ТЕМ ЖЕ поиском статьи, каким пишет save_lesson.

    ⚠️ Имя файла НЕ угадываем: `save_lesson` ищет статью через
    `find_existing_article` (семантика + slug), и самодельный путь разъехался бы
    с ней молча — замер писался бы в одну статью, а дельты читались из другой.
    ⚠️ Импорт ВНЕ try: опечатка в имени функции обязана падать громко. Ровно на
    этом месте она и случилась при написании модуля (звали несуществующий
    `slugify`), и немой `except Exception` оставил бы ряд без дельт навсегда —
    тот же класс «поле передано, потребитель не взял», что чинили в v1.71.1.
    """
    from memory_compiler.storage import find_existing_article

    path = find_existing_article(TOPIC, "", PROJECT)
    if not path or not path.exists():
        return None
    try:
        return parse_snapshot(path.read_text(encoding="utf-8"))
    except OSError:
        return None


async def run_async(hours: float = 24.0) -> str:
    """Снять замер и дописать его в базу. Возвращает текст записи.

    ⚠️ ШТАТНЫЙ ПУТЬ — ВНУТРИ РАБОТАЮЩЕГО СЕРВЕРА (`POST /api/metrics/daily`), а не
    вторым процессом python в том же контейнере. Первая редакция запускала
    `docker exec … python -m memory_compiler.daily_metrics`, и это было опасно
    вдвойне: во-первых, `embed_document` берёт ВНУТРИПРОЦЕССНЫЙ лок и переписывает
    pickle эмбеддингов целиком — параллельный процесс затёр бы то, что сервер
    посчитал за это время; во-вторых, тот процесс падал на выходе с кодом 134
    (`terminate called without an active exception` — уборка torch), и cron видел
    бы FAIL при успешно записанном замере, то есть провал и успех стали бы
    неотличимы.

    ⚠️ `daily()` читает мегабайтный аудит-лог — строго через `to_thread`, иначе
    встанет весь сервер (тот же класс, что ловили с `rerank` и `git_commit`).
    """
    from memory_compiler.analytics import daily
    from memory_compiler.handlers import save_lesson

    d = await asyncio.to_thread(daily, hours)
    prev = await asyncio.to_thread(_previous_snapshot)
    text = format_report(d, prev)
    await save_lesson(topic=TOPIC, content=text, project=PROJECT, tags=list(TAGS))
    return text


def run(hours: float = 24.0) -> str:
    """Синхронная обёртка — для ручного прогона ВНЕ работающего сервера.

    Годится, чтобы посмотреть цифры; для регулярного замера использовать REST
    (см. предупреждение в `run_async`).
    """
    return asyncio.run(run_async(hours))


if __name__ == "__main__":  # pragma: no cover
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("[%s] суточный замер" % stamp)
    print(run())
