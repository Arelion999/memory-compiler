"""Свежесть контекста между параллельными сессиями.

Задача: сессия A загрузила контекст утром, сессия B в обед изменила ту же
инфраструктуру и записала это в базу. Сессия A об этом не узнает никогда —
`start_task` в ней уже отработал, повторять его модели незачем, и она начинает
разбираться с чужими изменениями с нуля. Живой случай владельца (2026-08-26,
работа с MikroTik в двух сессиях подряд).

Решение: сервер сам знает про все сессии, потому что все записи идут через него.
Он держит снимок «что эта сессия уже видела» по паре (сессия, проект) и при
следующем вызове дописывает к ответу, что появилось у ДРУГИХ сессий с тех пор.

⚠️ ПОЧЕМУ ЭТО НА СЕРВЕРЕ, А НЕ В ХУКАХ КЛИЕНТА. Та же проверка сначала была
сделана хуками Claude Code — и работала ровно на одной машине с одним клиентом.
Здесь она достаётся любому клиенту (Claude Desktop, IDE, чужой MCP-клиент) без
настройки, потому что едет вместе с ответом инструмента.

⚠️ СОСТОЯНИЕ В ПАМЯТИ, И ЭТО ОСОЗНАННО. Рестарт контейнера обнуляет буфер — но
он же рвёт MCP-сессии всех клиентов (см. статью про -32001), то есть снимки
и так теряют смысл. Писать на диск было бы дороже и бессмысленнее.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any
from weakref import WeakKeyDictionary

# Последние записи в базу: (ts, project, tool, topic, session_key).
_writes: deque = deque(maxlen=300)
# Что каждая сессия уже видела: (session_key, project) -> ts.
_seen: dict[tuple[str, str], float] = {}
# Последний проект, с которым работала сессия, — для вызовов без project.
_last_project: dict[str, str] = {}
# Отсчёт молчания: (session_key, project) -> ts начала работы либо своей записи.
_started: dict[tuple[str, str], float] = {}
# Стабильные ключи сессий: id() переиспользуется после сборки мусора.
_keys: WeakKeyDictionary = WeakKeyDictionary()
_counter = [0]

# Старше этого записи не показываем: «изменилось вчера» — это не новость, а
# нормальная история проекта, за ней идут в timeline.
MAX_AGE_SEC = 12 * 3600
MAX_SHOWN = 5
# Потолок на словари: сессий за сутки бывают десятки, чистим самые старые.
MAX_SEEN = 500
# Молчаливая работа дольше этого — повод напомнить про заметку по ходу.
# Замер 2026-08-26 по аудиту: работа после последней загрузки контекста —
# медиана 25 минут, p90 101. Инструмент, о котором надо ВСПОМНИТЬ, механизмом
# не работает: у stale_facts за 4.5 месяца ноль вызовов.
NOTE_HINT_SEC = 25 * 60


def key_for(session: Any) -> str:
    """Стабильный ключ MCP-сессии. Пустая строка = вне запроса (тесты, REST)."""
    if session is None:
        return ""
    try:
        key = _keys.get(session)
        if key is None:
            _counter[0] += 1
            key = "s%d" % _counter[0]
            _keys[session] = key
        return key
    except TypeError:
        # Объект без поддержки weakref — id() как запасной вариант.
        return "i%x" % id(session)


def note_write(project: str, tool: str, topic: str, key: str) -> None:
    if not project or project == "all":
        return
    _writes.append((time.time(), project, tool, (topic or "")[:120], key))
    if key:
        # своя запись сдвигает отсчёт молчания: напоминать сразу после того, как
        # просьбу выполнили, — верный способ обесценить напоминание
        _started[(key, project)] = time.time()


def touch(key: str, project: str) -> None:
    """Отметить, что сессия видела состояние проекта на этот момент."""
    if not key or not project or project == "all":
        return
    _last_project[key] = project
    _seen[(key, project)] = time.time()
    if len(_seen) > MAX_SEEN:
        for k, _v in sorted(_seen.items(), key=lambda kv: kv[1])[:MAX_SEEN // 5]:
            _seen.pop(k, None)


def consume(key: str, project: str) -> str:
    """Что записали ДРУГИЕ сессии с прошлого вызова этой. Обновляет снимок.

    Первое касание проекта футера не даёт: сессия только что получила свежие
    данные, сообщать ей об их свежести незачем.
    """
    if not key:
        return ""
    if not project or project == "all":
        project = _last_project.get(key, "")
    if not project:
        return ""

    last = _seen.get((key, project))
    touch(key, project)
    if last is None:
        _started.setdefault((key, project), time.time())
        return ""
    hint = _note_hint(key, project)

    now = time.time()
    fresh = [w for w in _writes
             if w[1] == project and w[0] > last and w[4] != key and now - w[0] <= MAX_AGE_SEC]
    if not fresh:
        return hint

    lines = []
    for ts, _proj, tool, topic, _k in fresh[-MAX_SHOWN:]:
        stamp = time.strftime("%H:%M", time.localtime(ts))
        lines.append("- [%s] %s: %s" % (stamp, tool, topic or "(без темы)"))
    more = len(fresh) - len(lines)
    if more > 0:
        lines.append("- …и ещё %d" % more)
    return (
        "\n\n⚠️ **Пока вы работали, в проекте `%s` писала другая сессия** "
        "(%d запис%s). Инфраструктура могла измениться под вами:\n%s\n"
        "Прежде чем объяснять расхождения или чинить — перечитайте: "
        "`get_active_context` / `read_article` по этим темам. Скорее всего, "
        "изменения уже описаны, и разбираться с нуля не нужно."
        % (project, len(fresh), _plural(len(fresh)), "\n".join(lines))
    ) + hint


def _note_hint(key: str, project: str) -> str:
    """Напомнить про `session_note`, если сессия давно работает и молчит.

    Отсчёт — от начала работы с проектом либо от последней СВОЕЙ записи.
    Напоминание сдвигает отсчёт: подсказка, повторяемая в каждом ответе,
    читается как шум и перестаёт работать.
    """
    since = _started.get((key, project))
    if since is None:
        _started[(key, project)] = time.time()
        return ""
    if time.time() - since < NOTE_HINT_SEC:
        return ""
    _started[(key, project)] = time.time()
    return (
        "\n\n💡 Работа по `%s` идёт больше %d минут без записи в базу. Если по "
        "дороге что-то выяснилось — `session_note` (одна строка, сводка сессии "
        "не пересобирается): параллельная сессия и следующий старт это увидят."
        % (project, NOTE_HINT_SEC // 60)
    )


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ь"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "и"
    return "ей"


def reset() -> None:
    """Только для тестов: состояние модульное и переживает между ними."""
    _writes.clear()
    _seen.clear()
    _last_project.clear()
    _started.clear()
