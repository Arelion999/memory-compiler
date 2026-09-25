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

⚠️ СНИМКИ ЧАТОВ ПЕРЕЖИВАЮТ РЕСТАРТ (v1.88.1). Раньше всё состояние жило в памяти с
доводом «рестарт контейнера всё равно рвёт MCP-сессии, снимки теряют смысл». Довод
устарел в v1.76.0: ключ снимка — id чата от клиента (c:<id>), и после рестарта чат
тот же. Итог — «Первое обращение к проекту» приходило заново после КАЖДОГО рестарта,
а watcher перезапускает контейнер десятки раз в день (живые случаи 15.09.2026).
Теперь _seen для ключей c:<id> пишется в STATE_PATH. Путь задаёт lifespan: временный
каталог контейнера переживает docker restart и теряется лишь при пересоздании.
⚠️ Не в базе знаний: .gitignore базы правят руками, и файл в /knowledge уезжал бы в
каждый git add -A. ⚠️ Ключи MCP-сессий (s1, s2…) НЕ сохраняются: счётчик после
рестарта начинается заново, и новая сессия унаследовала бы чужой снимок. Буфер
записей и отсчёт молчания остаются в памяти — после рестарта теряются только
новости о записях, сделанных ДО него.

⚠️ РАБОЧИЙ ПРОЕКТ ЧАТА ТОЖЕ ПЕРЕЖИВАЕТ РЕСТАРТ (v1.92.6). Иначе после рестарта, до
первой записи чата, подстановка пропущенного project брала последний ПРОЧИТАННЫЙ
проект — класс ошибки, закрытый v1.90.2 только для работающего процесса. Тот же файл,
формат {"seen": […], "work": […]}; список троек v1.88.1 читается как seen.
"""

from __future__ import annotations

import json
import time
from collections import deque
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable
from weakref import WeakKeyDictionary

# Последние записи в базу: (ts, project, tool, topic, session_key).
_writes: deque = deque(maxlen=300)
# Что каждая сессия уже видела: (session_key, project) -> ts.
_seen: dict[tuple[str, str], float] = {}
# Последний проект, которого касалась сессия, — чтением или записью.
_last_project: dict[str, str] = {}
# Рабочий проект сессии: куда она писала или где звала start_task. Чтение его не
# перебивает (см. last_project). Для чатов (c:<id>) переживает рестарт — см. claim.
_work_project: dict[str, str] = {}
# Когда рабочий проект подтверждён последний раз: TTL в файле считается от него.
_work_ts: dict[str, float] = {}
# Отсчёт молчания: (session_key, project) -> ts начала работы либо своей записи.
_started: dict[tuple[str, str], float] = {}
# Стабильные ключи сессий: id() переиспользуется после сборки мусора.
_keys: WeakKeyDictionary = WeakKeyDictionary()
_counter = [0]

# Что чат уже видел в start_task ЦЕЛИКОМ (v1.92.0): (ключ, проект) -> {метка: время}.
# ⚠️ Только в памяти: после рестарта выдача снова полная — безопасная сторона ошибки,
# файл для этого не заводим.
_shown: dict[tuple[str, str], dict[str, float]] = {}

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
# Файл снимков чатов (см. докстринг модуля). None — не сохранять: так в тестах и вне
# сервера; путь выставляет lifespan в api.py.
STATE_PATH: Path | None = None
# Снимок чата, молчавшего дольше, забываем: такой чат и так начинает с чистого листа.
SEEN_TTL_SEC = 7 * 24 * 3600
# Файл освежается не чаще этого: TTL в нём обязан считаться от последней активности.
SAVE_EVERY_SEC = 3600
_loaded = [False]
_last_save = [0.0]

# Окно, в котором повторный start_task показывает уже виденное следом (v1.92.0).
# Замер 25.09.2026 по транскриптам за 14 дней: 101 повтор в том же чате и проекте из
# 310 вызовов; в пределах 3 ч — 77, из них после сжатия контекста на клиенте только 3.
# Сервер о сжатии не знает, поэтому окно короткое, а повтор идёт следом, а не пропадает.
SHOWN_WINDOW_SEC = 3 * 3600
# Потолок пар «чат–проект»: сверх него забываем самые давние по последнему показу.
MAX_SHOWN_PAIRS = 200
# Ключ чата текущего вызова (v1.92.0). Кладёт tools.call_tool перед хендлером и только
# вида c:<id>: у моста Claude Desktop одна MCP-сессия на все чаты Code, и по общему ключу
# один чат прятал бы пункты от другого. ContextVar привязан к задаче — параллельные
# вызовы не путаются (тот же приём, что search_payload_var).
chat_key_var: ContextVar[str] = ContextVar("mc_chat_key", default="")

# Служебный аргумент вызова — id чата на стороне клиента (v1.76.0). call_tool
# вынимает его до аудита и хендлера. Зачем: Claude Desktop отдаёт чатам Code
# серверы из claude_desktop_config.json через свой мост (mcp-remote), и у ВСЕХ
# таких чатов одна MCP-сессия. Ключ по объекту сессии склеивал их снимки: новый
# чат на первом касании получал «25 минут без записи» от чужой работы (замер
# 2026-09-11). Через мост доезжают только аргументы — заголовки у mcp-remote
# статические, а Mcp-Session-Id клиенты не переиспользуют, так что сброс по
# initialize этого не лечит.
CLIENT_SESSION_ARG = "_client_session"
_CLIENT_SESSION_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
_CLIENT_SESSION_MAX = 128


def key_for(session: Any, client_session: Any = None) -> str:
    """Стабильный ключ сессии для снимка свежести. Пустая строка = вне запроса.

    Id чата от клиента важнее объекта MCP-сессии: он различает чаты за общим
    мостом и переживает переподключение и смену маршрута. Кривой id молча
    игнорируется — ключ остаётся прежним, по MCP-сессии.
    """
    if (isinstance(client_session, str)
            and 0 < len(client_session) <= _CLIENT_SESSION_MAX
            and set(client_session) <= _CLIENT_SESSION_CHARS):
        return "c:" + client_session
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


def is_first_touch(key: str, project: str) -> bool:
    """Впервые ли эта сессия обращается к проекту.

    Спрашивать ОБЯЗАТЕЛЬНО до `consume` — тот делает `touch` и признак стирает.
    """
    if not key or not project or project == "all":
        return False
    _ensure_loaded()
    return (key, project) not in _seen


def touch(key: str, project: str) -> None:
    """Отметить, что сессия видела состояние проекта на этот момент."""
    if not key or not project or project == "all":
        return
    _ensure_loaded()
    _last_project[key] = project
    new = (key, project) not in _seen
    _seen[(key, project)] = time.time()
    if len(_seen) > MAX_SEEN:
        for k, _v in sorted(_seen.items(), key=lambda kv: kv[1])[:MAX_SEEN // 5]:
            _seen.pop(k, None)
    # На диск — новая пара сразу, остальное не чаще раза в SAVE_EVERY_SEC. Писать только
    # новые пары мало: TTL в файле считался бы от ПЕРВОГО касания, и чат, неделю работающий
    # с одним проектом без новых пар на сервере, после рестарта снова стал бы новым
    # (ревью 15.09.2026).
    if key.startswith("c:") and (new or time.time() - _last_save[0] >= SAVE_EVERY_SEC):
        _save_seen()


def _live_triples(items: Any, now: float):
    """Записи [ключ чата, проект, ts] из файла: целые, чатов (c:<id>), не старше TTL."""
    for item in (items if isinstance(items, list) else []):
        try:
            key, project, ts = item
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if (isinstance(key, str) and key.startswith("c:") and isinstance(project, str)
                and project and now - ts <= SEEN_TTL_SEC):
            yield key, project, ts


def _ensure_loaded() -> None:
    """Подтянуть с диска снимки и рабочие проекты чатов, сделанные до рестарта. Один
    раз на процесс.

    Файл — {"seen": [[ключ, проект, ts]…], "work": [[ключ, проект, ts]…]}. Файл v1.88.1 —
    просто список троек снимков: он читается как seen, рабочих проектов в нём нет.
    ⚠️ При откате на код до этого формата словарь не прочитается, и каждый чат один раз
    получит «Первое обращение к проекту» — безопасная сторона ошибки."""
    if _loaded[0]:
        return
    _loaded[0] = True
    if STATE_PATH is None:
        return
    try:
        state = json.loads(Path(STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return                            # нет файла или битый — с чистого листа
    if isinstance(state, dict):
        seen, work = state.get("seen"), state.get("work")
    else:
        seen, work = state, []
    now = time.time()
    for key, project, ts in _live_triples(seen, now):
        _seen.setdefault((key, project), ts)
    for key, project, ts in _live_triples(work, now):
        _work_project.setdefault(key, project)
        _work_ts.setdefault(key, ts)


def _save_seen() -> None:
    """Снимки и рабочие проекты чатов на диск. ⚠️ Синхронно, на loop: dumps итерирует
    _seen, а его мутируют соседние вызовы инструментов, — в потоке это гонка (LOOP_ONLY в
    tests/test_no_blocking_calls.py). Файл маленький: не больше MAX_SEEN пар каждого вида."""
    if STATE_PATH is None:
        return
    # ⚠️ Файл сначала читается: первым после рестарта может прийти claim, и запись без
    # чтения затёрла бы снимки и рабочие проекты всех остальных чатов.
    _ensure_loaded()
    from memory_compiler.config import atomic_write_text
    now = time.time()
    state = {"seen": [[k, p, ts] for (k, p), ts in _seen.items() if k.startswith("c:")],
             "work": [[k, p, _work_ts.get(k, now)] for k, p in _work_project.items()
                      if k.startswith("c:")]}
    try:
        Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False))
        _last_save[0] = time.time()
    except OSError:
        pass                              # сторож не имеет права ронять вызов


def claim(key: str, project: str) -> None:
    """Сессия работает с проектом: пишет в него или открыла по нему задачу.

    ⚠️ Рабочий проект чата (c:<id>) ПЕРЕЖИВАЕТ РЕСТАРТ (решение владельца 25.09.2026):
    иначе после рестарта, до первой записи, подстановка project брала последний
    прочитанный проект. Смена проекта ложится на диск сразу, а не по часовому таймеру:
    пара (чат, проект) могла быть уже видена чтением, и touch её не сохранил бы — после
    рестарта вернулся бы ПРЕЖНИЙ проект, и запись ушла бы в него."""
    if not key or not project or project == "all":
        return
    changed = _work_project.get(key) != project
    _work_project[key] = project
    _work_ts[key] = time.time()
    if len(_work_project) > MAX_SEEN:
        for k, _ts in sorted(_work_ts.items(), key=lambda kv: kv[1])[:MAX_SEEN // 5]:
            _work_project.pop(k, None)
            _work_ts.pop(k, None)
    if changed and key.startswith("c:"):
        _save_seen()


def last_project(key: str) -> str:
    """Проект сессии для вызова без project: рабочий, иначе последний тронутый.

    ⚠️ ЧТЕНИЕ НЕ ПЕРЕБИВАЕТ РАБОЧИЙ ПРОЕКТ (24.09.2026). Раньше ответом был
    последний тронутый проект, а сессия трогает проект и на чистом чтении:
    работала с A, для справки прочла статью из B — и запись без project ушла в
    B. Пока сессия ничего не писала и не звала start_task, сильнее последнего
    чтения сигнала нет, и тогда ответ прежний. Пустая строка — сессия ещё ни
    одного проекта не открывала, подставлять нечего.
    """
    if not key:
        return ""
    _ensure_loaded()
    return _work_project.get(key) or _last_project.get(key, "")


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

    _ensure_loaded()
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


def shown_recently(key: str, project: str) -> set[str]:
    """Метки пунктов start_task, которые чат видел ЦЕЛИКОМ за SHOWN_WINDOW_SEC.

    Попутно вычищает просроченное, чтобы память не росла на долгих чатах.
    """
    if not key or not project:
        return set()
    bucket = _shown.get((key, project))
    if not bucket:
        return set()
    now = time.time()
    fresh = {m: ts for m, ts in bucket.items() if now - ts <= SHOWN_WINDOW_SEC}
    if len(fresh) != len(bucket):
        if fresh:
            _shown[(key, project)] = fresh
        else:
            _shown.pop((key, project), None)
    return set(fresh)


def remember_shown(key: str, project: str, idents: Iterable[str | None]) -> None:
    """Отметить пункты, показанные чату ЦЕЛИКОМ.

    ⚠️ След сюда не попадает: он не продлевает окно, и полный текст повторяется не
    реже раза в SHOWN_WINDOW_SEC — иначе после сжатия контекста на клиенте пункт мог
    бы жить одним следом бесконечно.
    """
    if not key or not project:
        return
    idents = [m for m in idents if m]
    if not idents:
        return
    now = time.time()
    bucket = _shown.setdefault((key, project), {})
    for m in idents:
        bucket[m] = now
    if len(_shown) > MAX_SHOWN_PAIRS:
        by_age = sorted(_shown.items(), key=lambda kv: max(kv[1].values()))
        for pair, _bucket in by_age[:len(_shown) - MAX_SHOWN_PAIRS]:
            _shown.pop(pair, None)


def reset() -> None:
    """Только для тестов: состояние модульное и переживает между ними."""
    _writes.clear()
    _seen.clear()
    _last_project.clear()
    _work_project.clear()
    _work_ts.clear()
    _started.clear()
    _shown.clear()
    _loaded[0] = False                    # как рестарт: файл снимков на диске остаётся
    _last_save[0] = 0.0
