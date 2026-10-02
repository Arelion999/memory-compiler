#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Сторож памяти memory-compiler для хуков Claude Code и Kimi Code.

ЕДИНАЯ кодовая база для обоих клиентов. Источник — hooks/mc_guard.py репозитория
memory-compiler (публичный, поэтому никаких боевых адресов и путей машины:
значения приходят из os.environ или файла mc_guard.env рядом с установленной
копией). Установка копий в ~/.claude/hooks/ и ~/.kimi-code/hooks/ и генерация
секций хуков в конфигах клиентов — install.py из того же каталога.

Профиль клиента (detect_client): argv --client=claude|kimi → env MC_GUARD_CLIENT
→ поле события client_type → эвристика (tool_call_id без tool_use_id, prompt
списком) → fallback claude. От профиля зависят ТОЛЬКО: каталоги состояния
(STATE_DIR/HOOK_LOG/PENDING_DIR), режим emit() для информационных сообщений
(kimi — простой текст, claude — JSON) и способ блокировки Stop (_block:
kimi — причина в stderr + exit 2, claude — JSON decision). Всё остальное общее.

Основные режимы (подкоманда первым аргументом; полный список — COMMANDS):

  mark       PostToolUse на mcp__memory-compiler__*  — отмечает, что сессия
             обращалась к базе: время, проект, точка синхронизации с аудит-логом.
  gate       PreToolUse на инструментах живой инфраструктуры — не пускает на
             железо, пока в сессии не было ЧТЕНИЯ базы за последние FRESH_SEC.
  freshness  UserPromptSubmit — правило порядка + предупреждение, если ДРУГАЯ
             сессия писала в базу после того, как эта загрузила контекст.
  stop       Stop — блок только по делу: фраза «нет доступа» без похода в базу,
             либо забытый finish_task.
  session_arg PreToolUse на mcp__memory-compiler__* — кладёт id чата в аргументы
             вызова: сервер (v1.76.0+) считает по нему свежесть контекста, а не по
             MCP-сессии, общей у чатов за мостом Claude Desktop.
  reflex     PostToolUseFailure и PostToolUse(Read) — памятки из базы по ошибке и
             файлу (сервер v1.78.0+); цель выхода на железо — внутри gate.
  nul_guard  PreToolUse на Bash/PowerShell — блок редиректа в зарезервированное
             имя Windows (nul/con/aux/...) через JSON permissionDecision=deny
             (одинаково в обоих клиентах).
  compact    PostCompact — напоминание про save_compact (перенесено из
             inline-echo конфига: там кириллица регулярно билась в mojibake).

Состояние сессии: <профильный каталог>/state/<session_id>.json

Почему счёт «чужих» записей идёт по ts ИЗ аудит-лога, а не по mtime файлов:
локальное зеркало knowledge/ приезжает через Synology Drive с задержкой, и
mtime там проставляет mc-drive-nudge.sh пачками — то есть mtime говорит о
моменте синка, а не правки. ts в _audit.log ставит сервер в момент вызова
(TZ Asia/Vladivostok = локальная +1000), поэтому своя запись, доехавшая позже,
всё равно оказывается СТАРШЕ отметки seen и в предупреждение не попадает.
Пути к аудит-логу может не быть вовсе (KNOWLEDGE_DIR не задан) — тогда
свежесть/счётчики молча не работают, остальное функционирует.
"""

import hashlib
import ipaddress
import json
import os
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE") or Path.home())

# ------------------------------------------------------- локальный env-файл
# mc_guard.env рядом с установленной копией скрипта (машинно-локальный, в репо
# не попадает) — слой значений МЕЖДУ os.environ и нейтральными дефолтами.
# Формат: KEY=VALUE по строкам, # — комментарий, пустые строки пропускаются.
_LOCAL_ENV_PATH = Path(__file__).resolve().parent / "mc_guard.env"
_LOCAL_ENV_CACHE = None


def _local_env():
    global _LOCAL_ENV_CACHE
    if _LOCAL_ENV_CACHE is None:
        data = {}
        try:
            for line in _LOCAL_ENV_PATH.read_text(
                    encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip()] = value.strip()
        except Exception:
            pass
        _LOCAL_ENV_CACHE = data
    return _LOCAL_ENV_CACHE


def _reset_local_env():
    """Сброс кэша mc_guard.env — для тестов."""
    global _LOCAL_ENV_CACHE
    _LOCAL_ENV_CACHE = None


def _conf(name, default=None):
    """Значение настройки: os.environ → mc_guard.env рядом со скриптом → default."""
    value = os.environ.get(name)
    if value:
        return value
    return _local_env().get(name) or default


# ------------------------------------------------------- профиль клиента
# Единый скрипт обслуживает Claude Code и Kimi Code. От профиля зависят ТОЛЬКО
# три вещи: каталоги состояния (STATE_DIR/HOOK_LOG/PENDING_DIR), режим emit()
# для информационных сообщений и способ блокировки Stop (_block/cmd_stop).
# Глобалы читаются в момент вызова: main() разрешает профиль до диспатча,
# тесты задают его _set_client().
_CLIENT_CACHE = None
_EMIT_PLAIN = False          # kimi: информационные сообщения — простым текстом


def detect_client(event=None):
    """(клиент, источник): argv --client= → env MC_GUARD_CLIENT → client_type
    события → эвристика (tool_call_id без tool_use_id, prompt списком) →
    fallback claude."""
    for arg in sys.argv[1:]:
        if arg.startswith("--client="):
            value = arg.split("=", 1)[1].strip().lower()
            if "kimi" in value:
                return "kimi", "argv"
            if "claude" in value:
                return "claude", "argv"
    value = (os.environ.get("MC_GUARD_CLIENT") or "").strip().lower()
    if "kimi" in value:
        return "kimi", "env"
    if "claude" in value:
        return "claude", "env"
    if isinstance(event, dict):
        ctype = str(event.get("client_type") or "").lower()
        if "kimi" in ctype:
            return "kimi", "event"
        if "claude" in ctype:
            return "claude", "event"
        if (event.get("tool_call_id") and not event.get("tool_use_id")) \
                or isinstance(event.get("prompt"), list):
            return "kimi", "heuristic"
    return "claude", "fallback"


def _apply_client(client):
    global STATE_DIR, HOOK_LOG, PENDING_DIR, _EMIT_PLAIN, _CLIENT_CACHE
    root = HOME / (".kimi-code" if client == "kimi" else ".claude") / "hooks"
    STATE_DIR = root / "state"
    HOOK_LOG = root / "mc_hooks.log"
    PENDING_DIR = root / "pending"
    _EMIT_PLAIN = client == "kimi"
    _CLIENT_CACHE = client


def _set_client(client):
    """Жёстко задать профиль (тесты; клиент при необходимости)."""
    _apply_client("kimi" if "kimi" in str(client).lower() else "claude")


def _reset_client():
    """Сбросить кэш детекта — следующий _ensure_client определит заново."""
    global _CLIENT_CACHE
    _CLIENT_CACHE = None


def _ensure_client(event=None):
    """Ленивая разрешение профиля: первый вызов определяет и применяет."""
    if _CLIENT_CACHE is None:
        client, source = detect_client(event)
        _apply_client(client)
        if source == "fallback":
            journal(event if isinstance(event, dict) else {}, "client.fallback",
                    detail="сигнатур клиента нет, выбран claude")


# Состояние, журнал и очередь разнесены по клиентам сознательно (не сливаем):
# ~/.claude/hooks/... и ~/.kimi-code/hooks/... — свои копии у каждого клиента.
STATE_DIR = HOME / ".claude" / "hooks" / "state"

# Каталог базы знаний на этой машине: os.environ MC_KNOWLEDGE_DIR →
# mc_guard.env → None. Без него функции, читающие аудит-лог (свежесть,
# продуктовые счётчики), тихо деградируют — остальное работает.
_kd = _conf("MC_KNOWLEDGE_DIR")
KNOWLEDGE_DIR = Path(_kd) if _kd else None
AUDIT_LOG = (KNOWLEDGE_DIR / "_audit.log") if KNOWLEDGE_DIR is not None else None

# Сколько времени обращение к базе считается свежим для гейта.
FRESH_SEC = 15 * 60
# Аварийный клапан: столько подряд блокировок, дальше пропускаем (иначе при
# лежащей NAS гейт запер бы всю работу).
MAX_BLOCKS = 2
# Хвост аудит-лога, который читаем (файл на проде — мегабайты).
AUDIT_TAIL_BYTES = 300000

# Инструменты memory-compiler, считающиеся ЧТЕНИЕМ базы.
READ_TOOLS = {
    "search", "search_by_tag", "search_error", "search_decisions", "search_snippets",
    "read_article", "get_context", "get_active_context", "get_current", "get_summary",
    "ask", "start_task", "load_session", "get_runbook", "backlinks", "article_history",
    "stale_facts", "gap_report", "get_project_deps", "list_projects", "route_project",
}
# Инструменты, которые ПИШУТ в базу (для детектора чужих изменений).
WRITE_TOOLS = {
    "save_lesson", "save_decision", "save_runbook", "save_secret", "save_tracking",
    "save_session", "save_from_template", "save_contexts", "save_compact",
    "finish_task", "edit_article", "delete_article", "compile", "consolidate", "ingest",
}

# Bash/PowerShell-команды, считающиеся выходом на чужое железо.
#
# ⚠️ Имя команды ищется ТОЛЬКО В ПОЗИЦИИ КОМАНДЫ и только с аргументом-целью.
# Наивное `\bssh\b` по всему тексту даёт ложные блокировки на командах, которые
# ПИШУТ про ssh, а не выполняют его: heredoc со статьёй, grep по слову, echo
# инструкции. Поймано на живом первом же запуске — гейт заблокировал правку
# собственного скила, где в тексте стояло «доступы пароль ssh».
REMOTE_CMD_RE = re.compile(
    r"(?:^|\n|[;&|`(]|\$\()\s*"
    r"(?:sudo\s+|nohup\s+|setsid\s+)*"
    r"(?:ssh|scp|sftp|plink|pscp|psexec|rdesktop|mstsc|winrm)\s+[\w.@$\"'\[-]"
    r"|Enter-PSSession|New-PSSession|Invoke-Command\s+-",
    re.IGNORECASE,
)
# Тело heredoc — это ДАННЫЕ, а не команды: анализируем только префикс до него.
HEREDOC_RE = re.compile(r"<<-?\s*['\"]?\w+")

# cmd-редирект в зарезервированное имя Windows (nul/con/aux/...). В bash и
# PowerShell «nul» — НЕ null-устройство, а обычный файл с зарезервированным
# именем: он вешает Synology Drive на «Обработка 1 файлов...» и не удаляется
# обычным del/Remove-Item. Случалось трижды.
NUL_REDIRECT_RE = re.compile(
    r"[0-9&]?>>?[\s\"\\]*(?:nul|con|prn|aux|com[1-9]|lpt[1-9])"
    r"(?:\.[a-z0-9]+)?(?:[^a-z0-9._-]|$)",
    re.IGNORECASE,
)
# Строковые литералы в команде — данные, а не редирект (для nul_guard).
QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")

# Фразы, после которых Stop требует отчёта: искал ли в базе.
EXCUSE_RE = re.compile(
    "нет\\s+доступа|не\\s+име[юе][^.]{0,20}доступ|доступа\\s+у\\s+меня\\s+нет"
    "|запусти\\s+сам|выполни\\s+сам|сделай\\s+сам|нужно\\s+выполнить\\s+вручную"
    "|не\\s+зна[юе][^.]{0,20}парол|нужен\\s+парол|нужны\\s+креды|нужны\\s+учётные"
    "|предостав[ьи][^.]{0,20}(?:парол|доступ|креды)"
    "|не\\s+могу\\s+подключиться|у\\s+меня\\s+нет\\s+(?:учётных|кред)",
    re.IGNORECASE,
)

# ⚠️ ЦИТАТА ОТКАЗА — НЕ ОТКАЗ. Замер 20.09.2026: за 26 дней журнала stop.excuse
# сработал 3 раза, и 2 из них — в сессии, которая ОБСУЖДАЛА этот механизм:
# фразы стояли в кавычках как цитата правила, а страж прочитал их как отказ
# работать и дважды заблокировал ход. Настоящее срабатывание за весь журнал
# одно. Ложная блокировка стоит хода работы, то есть дороже всего, что этот
# страж экономит, — поэтому кавычки и кодовые вставки из текста вырезаются
# ПЕРЕД проверкой. Вырезается только короткий фрагмент: непарная кавычка иначе
# съела бы половину ответа вместе с настоящим отказом.
_QUOTED_RE = re.compile(r"«[^»]{0,120}»|\"[^\"]{0,120}\"|`{1,3}[^`]{0,120}`{1,3}")


def _is_excuse(text):
    """Похож ли текст на отказ работать — не считая цитат и кода."""
    if not text:
        return False
    return bool(EXCUSE_RE.search(_QUOTED_RE.sub(" ", text)))


AUDIT_TS_FMT = "%Y-%m-%d %H:%M:%S"
# Допуск на расхождение часов машины и сервера при сравнении с аудит-логом.
CLOCK_SKEW_SEC = 90

# Журнал срабатываний самих хуков — источник статистики «что отработало».
HOOK_LOG = HOME / ".claude" / "hooks" / "mc_hooks.log"
HOOK_LOG_MAX = 2_000_000
# ⚠️ ПЕРЕПОЛНЕННЫЙ ЖУРНАЛ АРХИВИРУЕТСЯ, А НЕ ОБРЕЗАЕТСЯ. Прежняя ротация
# оставляла последние 5000 строк, остальное удаляя безвозвратно. Цена вскрылась
# 20.09.2026: в логе лежало 17 269 строк за 26 дней, и именно по ним мерились
# приживаемость живой карточки по дням и доля покрытых ошибок — после первой же
# ротации такой замер стал бы невозможен, причём молча. Держим два архива
# (.1 и .2), то есть потолок примерно втрое от HOOK_LOG_MAX.
HOOK_LOG_ARCHIVES = 2

# ------------------------------------------------------------------ очередь
# Незаписанное в базу не должно теряться: payload сохраняется ДО вызова, при
# успехе удаляется, при провале остаётся и досылается по REST — уже без модели.
PENDING_DIR = HOME / ".claude" / "hooks" / "pending"
# Адрес REST сервера: os.environ MC_API_URL → mc_guard.env → loopback-заглушка.
# Боевой адрес живёт только в машинном mc_guard.env (репо публичный).
API_URL = (_conf("MC_API_URL") or "http://127.0.0.1:8765").rstrip("/")
# Файл с MC_API_KEY: os.environ MC_ENV_FILE → mc_guard.env → None (ключ тогда
# берётся только из os.environ/mc_guard.env — см. _api_key).
_ef = _conf("MC_ENV_FILE")
ENV_FILE = Path(_ef) if _ef else None
# Сколько ждать, прежде чем досылать самим: сперва шанс повторить вызов модели.
FLUSH_MIN_AGE = 90
FLUSH_THROTTLE = 120
PENDING_TTL_DAYS = 14
# Чужая запись очереди старше этого считается брошенной. Живая сессия повторяет свой
# упавший вызов за минуты (Stop не отпускает её до трёх раз), а показывать её запись
# другим сессиям нельзя: 15.09.2026 посторонняя сессия послушно повторила чужой
# edit_article, и в статье лёг дубль.
ORPHAN_MIN_AGE = 30 * 60
# Свою запись, отбитую валидацией аргументов, снимает успех того же инструмента, если
# она моложе этого. Замер 25.09.2026 по транскриптам: 23 таких отказа, исправленный повтор
# каждого прошёл через 3–64 с; запас — на ToolSearch и чтение статьи перед повтором.
REJECTED_RETRY_WINDOW = 10 * 60

# Что REST умеет принять как статью (POST /api/save -> save_lesson).
REST_SAVABLE = {
    "save_lesson", "save_decision", "save_runbook", "save_from_template",
    "save_session", "save_compact", "finish_task",
}
# Пишут в базу, но REST-эквивалента нет — досылать может только модель.
MANUAL_ONLY = {"edit_article", "save_tracking", "save_contexts", "save_secret"}
# Секрет в открытом виде на диск не кладём — в очереди остаётся только факт попытки.
NO_PAYLOAD = {"save_secret"}


# Kimi Work (desktop, daimon) называет инструменты плагинов
# mcp__plugin-<plugin>_<server>__<tool>. Сводим к каноническому
# mcp__<server>__<tool> — вся остальная логика (INFRA_TOOL_RE, startswith
# на mcp__memory-compiler__, short_tool) после этого работает без правок.
# Сервер опознаётся ПОСЛЕДНИМ сегментом перед «__»: у плагинов id может
# содержать дефисы и подчёркивания (plugin-kimi-cu-win_win,
# plugin-task-master-ai_task-master-ai), а server id — нет.
PLUGIN_TOOL_RE = re.compile(
    r"^mcp__plugin-(?P<plugin>.+)_(?P<server>[^_]+)__(?P<tool>.+)$")


def normalize_tool_name(name):
    """mcp__plugin-<plugin>_<server>__<tool> -> mcp__<server>__<tool>."""
    if not isinstance(name, str) or not name:
        return name if isinstance(name, str) else ""
    m = PLUGIN_TOOL_RE.match(name)
    if not m:
        return name
    return "mcp__%s__%s" % (m.group("server"), m.group("tool"))


# Алиасы полей события у разных клиентов. Claude Code / Kimi Code шлют
# snake_case; ядро daimon внутри живёт в camelCase — хук плагина может
# получить и такой вариант. Сводим ОДИН раз на границе; при коллизии
# snake_case в приоритете (проверенный формат).
_EVENT_FIELD_ALIASES = {
    "toolName": "tool_name",
    "toolInput": "tool_input",
    "sessionId": "session_id",
    "hookEventName": "hook_event_name",
    "toolResponse": "tool_response",
    "toolOutput": "tool_output",
    "transcriptPath": "transcript_path",
}


def normalize_event(event):
    """Свести payload хука к канонической snake_case-схеме + нормализовать
    имя инструмента. Чистая функция: неизвестные ключи не трогает,
    не-dict возвращает как есть. Fail-open по построению."""
    if not isinstance(event, dict):
        return event
    for alias, canonical in _EVENT_FIELD_ALIASES.items():
        if alias in event and canonical not in event:
            event[canonical] = event[alias]
    if "tool_name" in event:
        event["tool_name"] = normalize_tool_name(event.get("tool_name"))
    return event


def read_event():
    """stdin хука — UTF-8 от Claude Code / Kimi Code, кодировку консоли не спрашиваем.

    ⚠️ На Windows python читает pipe в кодировке консоли (cp1251), и кириллица
    превращается в «Р”РѕРµР·Р¶Р°РµС‚». Пока хуки только читали ввод, это было
    незаметно; session_arg (11.09.2026) вернул такой ввод в updatedInput и
    испортил аргументы вызова, дошедшие до базы.
    """
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}
    if not isinstance(event, dict):
        return {}
    event = normalize_event(event)
    # Kimi Code шлёт UserPromptSubmit.prompt списком ContentPart, Claude Code —
    # строкой. Сводим к тексту, чтобы подкоманды работали в обоих мирах.
    prompt = event.get("prompt")
    if isinstance(prompt, list):
        event["prompt"] = "\n".join(
            str(p.get("text") or "") for p in prompt
            if isinstance(p, dict) and p.get("type") == "text")
    return event


def _tool_response(event):
    """Вывод инструмента: Kimi Code шлёт tool_output, Claude Code — tool_response.

    ⚠️ В Kimi tool_output обрезан до 2000 символов — сверка цитат в probe и
    маркеры записи в mark/nudge по длинным ответам могут не найтись."""
    resp = event.get("tool_response")
    return resp if resp is not None else event.get("tool_output")


def _error_text(event):
    """Текст ошибки инструмента: Claude Code шлёт error строкой, Kimi Code —
    объектом {"code": ..., "message": ...} (живой замер 28.09.2026: isinstance-гейт
    cmd_reflex молча пропускал ВСЕ ошибки Bash в Kimi — рефлексы по ошибкам не
    работали никогда)."""
    err = event.get("error")
    if isinstance(err, dict):
        err = err.get("message")
    return str(err or "")


def state_path(event):
    sid = str(event.get("session_id") or "nosession")
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:120]
    return STATE_DIR / (sid + ".json")


def load_state(event):
    p = state_path(event)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # Битый или чужой формат не должен ронять гейт и снятие записи из очереди (ревью 14.09.2026).
    return data if isinstance(data, dict) else {}


def _atomic_write(path, text):
    """Запись через временный файл и os.replace: параллельный процесс видит старый или
    новый файл целиком. write_text сначала обрезает файл, и читающий в этот миг хук
    получал {} — так терялось состояние гейта (ревью 13.09.2026: 12 из 20 раундов
    по 8 параллельных вызовов теряли last_read_ts)."""
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))   # Windows: файл открыт другим процессом
    try:
        tmp.unlink()
    except Exception:
        pass


def save_state(event, st):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(state_path(event), json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


def emit(payload):
    """Информационные сообщения — по профилю клиента, служебные ответы — JSON.

    Kimi Code не читает hookSpecificOutput.additionalContext: простой stdout хука
    (exit 0) показывается пользователю как есть, поэтому в профиле kimi любое
    сообщение, где в hookSpecificOutput кроме hookEventName и additionalContext
    ничего нет (правила, памятки, предупреждения — на любом событии), уходит
    текстом. В профиле claude информационные сообщения остаются JSON. Байтами
    UTF-8: консольная кодировка Windows на пути не стоит. Ответы с
    permissionDecision / updatedInput / decision — всегда JSON
    ensure_ascii=True: чистый ASCII доезжает в любой кодировке."""
    hso = payload.get("hookSpecificOutput") if isinstance(payload, dict) else None
    if (_EMIT_PLAIN
            and isinstance(hso, dict)
            and "additionalContext" in hso
            and not (set(hso) - {"hookEventName", "additionalContext"})):
        text = str(hso.get("additionalContext") or "")
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(text.encode("utf-8", errors="replace"))
            buf.flush()
        else:
            sys.stdout.write(text)
            sys.stdout.flush()
        return
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    sys.stdout.flush()


def short_tool(name):
    return name.split("__")[-1] if name else ""


def _rotate_hook_log():
    """Сдвинуть журнал в архив: .1 -> .2, текущий -> .1.

    Переименование, а не перезапись: обрезка «оставить последние N строк»
    удаляла историю, по которой считается вся статистика приживаемости.
    На Windows rename поверх существующего файла падает, поэтому цель
    удаляется заранее.
    """
    def arch(n):
        return HOOK_LOG.parent / (HOOK_LOG.name + ".%d" % n)

    for i in range(HOOK_LOG_ARCHIVES - 1, 0, -1):
        src, dst = arch(i), arch(i + 1)
        if src.exists():
            if dst.exists():
                dst.unlink()
            src.rename(dst)
    first = arch(1)
    if first.exists():
        first.unlink()
    HOOK_LOG.rename(first)


def journal(event, action, tool="", detail=""):
    """Одна строка на срабатывание — на этом стоит вся статистика.

    Пишем ВСЕГДА, в том числе про пропуски (gate/pass): без знаменателя нельзя
    отличить «гейт не мешает» от «гейт не работает» — а именно этот вопрос и
    задаётся, когда смотришь, что отрабатывает."""
    try:
        HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        if HOOK_LOG.exists() and HOOK_LOG.stat().st_size > HOOK_LOG_MAX:
            _rotate_hook_log()
        rec = {
            "ts": datetime.now().strftime(AUDIT_TS_FMT),
            "session": str(event.get("session_id") or "")[:8],
            "action": action,
            "tool": tool or short_tool(event.get("tool_name", "")),
        }
        if detail:
            rec["detail"] = str(detail)[:200]
        with HOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ------------------------------------------------------- очередь: хранилище
def _pending_key(event):
    """Ключ связи PreToolUse -> PostToolUse(+Failure). tool_use_id уникален; если
    клиент его не прислал, падаем на хэш от инструмента и аргументов.
    Kimi Code называет это поле tool_call_id — принимаем оба."""
    tid = event.get("tool_use_id") or event.get("tool_call_id")
    if tid:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", str(tid))[:120]
    import hashlib
    raw = json.dumps([event.get("tool_name"), event.get("tool_input")],
                     ensure_ascii=False, sort_keys=True)
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _pending_path(event):
    return PENDING_DIR / (_pending_key(event) + ".json")


def _pending_all():
    try:
        return sorted(PENDING_DIR.glob("*.json"))
    except Exception:
        return []


def _pending_load(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pending_drop(p):
    try:
        p.unlink()
    except Exception:
        pass


def _pending_split(event, paths=None):
    """(свои, брошенные чужие) записи очереди для сессии события.

    ⚠️ Очередь общая на машину, а записи в ней — чужие вызовы. Свежую запись ЖИВОЙ
    чужой сессии не показываем никому: её повторит владелец, а повтор со стороны пишет
    дубль (15.09.2026). Брошенную — старше ORPHAN_MIN_AGE или без сессии — показываем,
    иначе содержание пропало бы вместе с закрытым окном."""
    sid = str(event.get("session_id") or "")
    now = time.time()
    own, orphans = [], []
    for p in (_pending_all() if paths is None else paths):
        rec = _pending_load(p) or {}
        owner = str(rec.get("session") or "")
        if sid and owner == sid:
            own.append(p)
        elif not owner or now - float(rec.get("created") or 0) >= ORPHAN_MIN_AGE:
            orphans.append(p)
    return own, orphans


def _call_signature(tool, args):
    """Вызов строкой для сравнения попыток. Без _client_session: его дописывает хук
    session_arg, и у записи упавшей попытки этого поля может не быть."""
    if isinstance(args, dict):
        args = {k: v for k, v in args.items() if k != "_client_session"}
    return json.dumps([short_tool(tool or ""), args], ensure_ascii=False, sort_keys=True)


def _close_retried(event):
    """Успешный вызов снимает упавшие попытки ТОГО ЖЕ вызова: свои и брошенные.

    ⚠️ Связка очереди с вызовом идёт по tool_use_id, а у повтора он другой: успех снимал
    только свою запись, первая висела до сверки с аудитом, а зеркало аудита отстаёт на
    минуты — всё это время Stop требовал повторить уже записанное. Снимаем только полное
    совпадение аргументов: повтор с другим текстом — другой вызов, его содержание терять
    нельзя. Живую чужую попытку не трогаем: владелец должен узнать, что его вызов упал."""
    sig = _call_signature(event.get("tool_name"), event.get("tool_input"))
    own, orphans = _pending_split(event)
    closed = 0
    for p in own + orphans:
        rec = _pending_load(p) or {}
        if _call_signature(rec.get("tool"), rec.get("args")) == sig:
            _pending_drop(p)
            closed += 1
    return closed


def _args_rejected(rec):
    """Отбит ли вызов на валидации аргументов: -32602 или «Input validation error».
    Тело инструмента такой вызов не запускал — в базу он ничего не записал."""
    err = str(rec.get("last_error") or "").lower()
    return "-32602" in err or "input validation error" in err


def _close_rejected(event):
    """Успешный вызов снимает СВОИ свежие отказы валидации того же инструмента.

    ⚠️ Отказ валидации модель чинит, меняя аргументы: title → topic, summary →
    topic/content, facts списком → объектом. _close_retried такой повтор не узнаёт, а
    _already_in_audit ищет по topic/filename, которых у отбитой записи нет, — и 23–25.09.2026
    SessionStart копил «БРОШЕНО ДРУГИМИ СЕССИЯМИ» (10, затем 16), хотя легла каждая.
    Снимается только отказ валидации: вызов ничего не записал. Таймаут, ENOENT, ошибка
    сервера — запись могла лечь частью, их снимает лишь полное совпадение аргументов.
    «could not be parsed» не снимаем: такой повтор дробят на части. Запись без last_error —
    вызов ещё в полёте. Чужие записи не трогаем — см. _pending_split."""
    tool = short_tool(event.get("tool_name", ""))
    own, _orphans = _pending_split(event)
    now = time.time()
    closed = 0
    for p in own:
        rec = _pending_load(p)
        if not isinstance(rec, dict) or short_tool(rec.get("tool", "")) != tool:
            continue
        if not _args_rejected(rec):
            continue
        try:
            age = now - float(rec.get("created") or 0)
        except (TypeError, ValueError):
            continue
        if age <= REJECTED_RETRY_WINDOW:
            _pending_drop(p)
            closed += 1
    return closed


# ------------------------------------------------------- очередь: доставка
def _api_key():
    """MC_API_KEY: os.environ → mc_guard.env рядом со скриптом → ENV_FILE."""
    key = os.environ.get("MC_API_KEY") or _local_env().get("MC_API_KEY")
    if key:
        return key
    if ENV_FILE is not None:
        try:
            for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MC_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""


def _rest_payload(rec):
    """Из аргументов MCP-вызова собираем то, что принимает POST /api/save.
    У finish_task полей больше, чем у статьи, — вклеиваем их в тело, иначе
    итог сессии и открытые вопросы потерялись бы при досыле."""
    args = rec.get("args") or {}
    topic = (args.get("topic") or args.get("filename") or "").strip()
    content = (args.get("content") or "").strip()
    if not topic or not content:
        return None
    extra = []
    if args.get("session_summary"):
        extra.append("**Итог сессии:** " + str(args["session_summary"]))
    if args.get("open_questions"):
        extra.append("**Открытые вопросы:** " + str(args["open_questions"]))
    if extra:
        content = content + "\n\n" + "\n\n".join(extra)
    content += ("\n\n_(досыл из локальной очереди mc_guard: вызов %s не дошёл "
                "до базы %s)_" % (rec.get("tool", "?"), rec.get("ts", "")))
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return {
        "topic": topic[:200],
        "content": content,
        "project": args.get("project") or "general",
        "tags": tags,
    }


def _post_save(payload):
    """POST /api/save. Тело кодируем сами: кириллица через shell-инлайн бьётся
    (проверено — curl --data с русским текстом дал 400 invalid json)."""
    import urllib.request
    import urllib.error
    key = _api_key()
    if not key:
        return False, "нет MC_API_KEY"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_URL + "/api/save", data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return (200 <= resp.status < 300), body[:200]
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code
    except Exception as e:
        return False, type(e).__name__


# ------------------------------------------------------------------ intent
def cmd_intent(event):
    """PreToolUse на пишущих инструментах: кладём payload в очередь ДО вызова.
    Если вызов не дойдёт до базы, содержание останется здесь, а не пропадёт."""
    tool = short_tool(event.get("tool_name", ""))
    args = event.get("tool_input") or {}
    if tool in NO_PAYLOAD:
        args = {"topic": args.get("topic"), "project": args.get("project"),
                "content": "", "_secret": True}
    rec = {
        "tool": tool,
        "args": args,
        "ts": datetime.now().strftime(AUDIT_TS_FMT),
        "created": time.time(),
        "session": event.get("session_id"),
        "attempts": 0,
        "manual_only": tool in MANUAL_ONLY,
    }
    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        _pending_path(event).write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    journal(event, "queue.add", tool=tool)
    return 0


# -------------------------------------------------------------------- fail
_FAIL_HINTS = (
    ("timed out", "-32001: почти всегда мёртвая MCP-сессия после рестарта "
                  "контейнера (их десятки в день). Лечится ПОВТОРОМ того же вызова."),
    ("-32001", "-32001: мёртвая сессия после рестарта контейнера. Повтори вызов."),
    ("validation", "Клиент отбил аргументы ещё до отправки — до сервера вызов не "
                   "дошёл. Проверь обязательные поля и объём content."),
    ("connect", "Сервер недоступен (NAS/сеть). Содержание сохранено в локальной "
                "очереди и будет дослано автоматически, но попробуй ещё раз."),
    ("econnrefused", "Сервер не принимает соединения. Payload в очереди, досыл "
                     "произойдёт сам; повтори вызов позже."),
)


def _raw_truncated(raw):
    """Оборван ли raw: не кончается на } или обрыв пришёлся внутрь строки (нечётные кавычки)."""
    text = raw.rstrip()
    if not text.endswith("}"):
        return True
    return len(re.findall(r'(?<!\\)"', text)) % 2 == 1


def _fail_hint(event, err):
    """Подсказка по классу отказа записи; err уже в нижнем регистре.

    ⚠️ Порядок проверок важен. В -32602 есть слово «validation», но вызов до сервера
    дошёл — общая подсказка «до сервера не дошёл» там врёт. «could not be parsed»
    бывает обрывом генерации (дробить content) и битым синтаксисом при целом raw
    (дробить бесполезно, 24.08.2026 так потеряли три попытки) — различаем по raw
    из обёртки __unparsedToolInput."""
    args = event.get("tool_input")
    args = args if isinstance(args, dict) else {}
    wrapped = args.get("__unparsedToolInput")
    raw = wrapped.get("raw") if isinstance(wrapped, dict) else None
    if "-32602" in err or "received undefined" in err:
        if wrapped is not None:
            return ("Аргументы ушли обёрткой __unparsedToolInput.raw, и сервер получил пустые поля. "
                    "Повтори вызов прямыми именованными параметрами: project, topic, content — "
                    "отдельными полями, tags — JSON-массив строк, кириллица литералом. "
                    "Сокращать текст не нужно.")
        fields = sorted(set(re.findall(r'"path":\s*\[\s*"([^"]+)"', err)))
        return ("Сервер отбил вызов: не передано обязательное поле %s. Добавь его и повтори."
                % (", ".join(fields) or "из path в тексте ошибки"))
    if "could not be parsed" in err:
        if isinstance(raw, str) and not _raw_truncated(raw):
            return ("raw целый, но это не JSON — ошибка синтаксиса, а не объёма. Типично: tags "
                    "голым текстом без [] и кавычек, псевдо-XML <parameter …> внутри строк. Зови "
                    "прямыми именованными параметрами, tags — JSON-массив строк. Сокращать текст "
                    "бесполезно, подсказку клиента про backslashes и truncated output игнорируй.")
        if isinstance(raw, str):
            return ("raw оборвался посреди текста — payload не влез в генерацию. ДРОБИ content, "
                    "повтор тем же объёмом упадёт снова.")
        return ("Аргументы не собрались в JSON. Если raw обрывается посреди слова — ДРОБИ content; "
                "если raw целый и кончается на } — это синтаксис (tags без [], псевдо-XML), "
                "сокращать бесполезно.")
    for needle, text in _FAIL_HINTS:
        if needle in err:
            return text
    return ("Разбери причину по тексту ошибки. Если это таймаут или обрыв "
            "связи — повтори вызов; если аргументы не собрались — дроби content.")


def cmd_fail(event):
    """PostToolUseFailure на пишущих инструментах: запись НЕ состоялась.
    Payload остаётся в очереди, модели говорим класс отказа и что делать."""
    tool = short_tool(event.get("tool_name", ""))
    p = _pending_path(event)
    rec = _pending_load(p)
    if rec is not None:
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["last_error"] = (_error_text(event) or str(_tool_response(event) or ""))[:400]
        try:
            p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    err = (_error_text(event) or str(_tool_response(event) or "")).lower()
    hint = _fail_hint(event, err)

    journal(event, "mc.fail", tool=tool, detail=err[:120])
    emit({"hookSpecificOutput": {
        "hookEventName": "PostToolUseFailure",
        "additionalContext": (
            "ЗАПИСЬ В БАЗУ НЕ ПРОШЛА (%s). Забивать нельзя: содержание сохранено "
            "в локальной очереди и будет дослано автоматически, но твоя задача — "
            "ПОВТОРИТЬ вызов сейчас (обычно со второго раза проходит). %s"
            % (tool, hint)
        ),
    }})
    return 0


# -------------------------------------------------------------------- flush
def _already_in_audit(rec):
    """Не висит ли запись в очереди зря: вызов мог пройти ПОВТОРОМ.

    ⚠️ Обязательная проверка, а не оптимизация. Связка очереди с вызовом идёт по
    tool_use_id, а у повторного вызова он ДРУГОЙ: успех повтора снимает свою
    отметку, а первая, упавшая, осталась бы в очереди навсегда — Stop блокировал
    бы ход до скончания века, а REST-досыл плодил бы дубли уже записанного.
    Аудит-лог приезжает с задержкой синка, поэтому очередь очищается не мгновенно
    — на это и рассчитан счётчик напоминаний в Stop."""
    args = rec.get("args") or {}
    key = (args.get("topic") or args.get("filename") or "").strip()
    if not key:
        return False
    tool = short_tool(rec.get("tool", ""))
    try:
        created = datetime.strptime(rec.get("ts", ""), AUDIT_TS_FMT)
    except Exception:
        return False
    for r in _tail_audit():
        if short_tool(r.get("tool", "")) != tool:
            continue
        a = r.get("args") or {}
        other = (a.get("topic") or a.get("filename") or "").strip()
        if other != key:
            continue
        try:
            ts = datetime.strptime(r.get("ts", ""), AUDIT_TS_FMT)
        except Exception:
            continue
        if (ts - created).total_seconds() >= -120:
            return True
    return False


def _flush_queue(force=False):
    """Досылаем очередь по REST — это работает, даже если модель забыла повторить.
    Возвращает (доставлено, осталось, список_описаний_остатка)."""
    items = _pending_all()
    if not items:
        return 0, 0, []

    st_file = STATE_DIR / "_flush.json"
    if not force:
        try:
            last = json.loads(st_file.read_text(encoding="utf-8")).get("ts", 0)
            if time.time() - float(last) < FLUSH_THROTTLE:
                return 0, len(items), _describe(items)
        except Exception:
            pass
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        st_file.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    except Exception:
        pass

    sent = 0
    now = time.time()
    for p in items:
        rec = _pending_load(p)
        if rec is None:
            _pending_drop(p)
            continue
        age_days = (now - float(rec.get("created") or now)) / 86400.0
        if age_days > PENDING_TTL_DAYS:
            _pending_drop(p)
            continue
        if _already_in_audit(rec):
            _pending_drop(p)              # вызов прошёл повтором — очередь закрыта
            continue
        if now - float(rec.get("created") or now) < FLUSH_MIN_AGE:
            continue                      # свежая — пусть модель повторит сама
        if rec.get("manual_only") or short_tool(rec.get("tool", "")) not in REST_SAVABLE:
            continue                      # REST такого не умеет — только модель
        payload = _rest_payload(rec)
        if not payload:
            continue
        ok, info = _post_save(payload)
        journal({}, "queue.sent" if ok else "queue.retry",
                tool=short_tool(rec.get("tool", "")),
                detail=(payload.get("topic", "")[:60] if ok else str(info)[:80]))
        if ok:
            _pending_drop(p)
            sent += 1
    left = _pending_all()
    return sent, len(left), _describe(left)


def _describe(paths):
    out = []
    for p in paths[:6]:
        rec = _pending_load(p) or {}
        args = rec.get("args") or {}
        out.append("%s -> %s/%s" % (
            rec.get("tool", "?"), args.get("project") or "?",
            (args.get("topic") or args.get("filename") or "?")[:70]))
    return out


def cmd_flush(event):
    sent, _left, _desc = _flush_queue()
    own, _orphans = _pending_split(event)    # чужую запись повторит её сессия
    if not own and not sent:
        return 0
    parts = []
    if sent:
        parts.append("Досланы в базу из локальной очереди: %d." % sent)
    if own:
        parts.append(
            "НЕ ЗАПИСАНО В БАЗУ: %d. Автодосыл возможен не для всего "
            "(edit_article, save_tracking, save_secret умеет только MCP-вызов) — "
            "повтори эти вызовы сам:\n%s" % (len(own), "\n".join(_describe(own))))
    emit({"hookSpecificOutput": {
        "hookEventName": event.get("hook_event_name") or "UserPromptSubmit",
        "additionalContext": " ".join(parts),
    }})
    return 0


# --------------------------------------------------------------------- mark
# Ответ сервера на запись статьи: путь стоит сразу за маркером действия. Двоеточие — вплотную
# к слову: «Статья не найдена: …» успехом не считается.
_WRITE_OK_RE = re.compile(r"(?:Создано|Обновлено|Статья|Дописано):\s*([\w.\-]+)/([^\s\"']+\.md)")
_VERIFY_ACCEPTED_RE = re.compile(r"Проверка: \+\d+")


def _refresh_card_after_verify(event, args):
    """Сервер принял цитату — забыть показанную карточку, чтобы следующий выход на узел взял
    у сервера ПРИНЯТЫЕ цитаты.

    ⚠️ Не кэшировать пары из аргументов вызова: сервер отвергает цитату (секрет без content,
    команда вне белого списка, креды, несуществующая статья), а локальный кэш делал из неё
    штамп verified — в том числе на секрете (ревью 14.09.2026, Critical). Истина о цитатах —
    ответ сервера на следующую карточку.
    ⚠️ Забываются ВСЕ ключи целей: ключ — хэш списка целей гейта, по статье его не
    восстановить. Статью убираем и из shown — и это НЕ побочный эффект: следующая карточка
    по узлу вернёт её СНОВА, уже с принятой цитатой, и только так свежая цитата доезжает до
    сверки (ревью 14.09.2026, N6). Цена — один повторный показ карточки этой статьи."""
    resp = str(_tool_response(event) or "")
    if not _VERIFY_ACCEPTED_RE.search(resp):
        return
    m = _WRITE_OK_RE.search(resp)
    # Путь — из ответа сервера: у save_lesson имя файла придумывает сервер, проект он
    # нормализует сам.
    project = (m.group(1) if m else str(args.get("project") or "")).strip().lower()
    filename = (m.group(2) if m else str(args.get("filename") or "")).strip()
    article = "%s/%s" % (project, filename)

    def _forget(st):
        st["keys"] = [k for k in _as_list(st.get("keys"))
                      if isinstance(k, str) and not k.startswith("target:")]
        # Проект в shown лежит так, как прислал сервер, а путь статьи приведён к нижнему
        # регистру: сравниваем без регистра, иначе карточка не забудется (ревью 14.09.2026, M1).
        st["shown"] = [s for s in _as_list(st.get("shown"))
                       if not (isinstance(s, str) and s.strip().lower() == article)]

    if _reflex_state_update(event, _forget) is not None:
        journal(event, "verify.added", detail=article[:80])


def cmd_mark(event):
    tool = short_tool(event.get("tool_name", ""))
    st = load_state(event)
    now = time.time()
    st["last_mc_ts"] = now
    st["last_mc_tool"] = tool
    if tool in READ_TOOLS:
        st["last_read_ts"] = now
    if tool in WRITE_TOOLS:
        st["did_write"] = True
    if tool == "start_task":
        st["started"] = True
    if tool == "finish_task":
        st["finished_ts"] = now
    # Точка синхронизации с аудит-логом: всё, что записано ДО этого момента,
    # считаем увиденным (своя запись имеет ts <= now).
    st["seen_audit_ts"] = datetime.now().strftime(AUDIT_TS_FMT)
    st["blocks"] = 0
    if tool in WRITE_TOOLS:
        # Запоминаем СВОИ записи поимённо. Одной отметки времени мало: сервер
        # ставит ts своими часами, и расхождение в секунду показывало собственный
        # finish_task как «правку из другой сессии» (поймано живьём 2026-08-26).
        args = event.get("tool_input") or {}
        key = "%s|%s" % (tool, (args.get("topic") or args.get("filename") or "").strip())
        mine = [k for k in (st.get("mine") or []) if k != key]
        mine.append(key)
        st["mine"] = mine[-40:]
    proj = (event.get("tool_input") or {}).get("project")
    if isinstance(proj, str) and proj and proj != "all":
        st["project"] = proj
    save_state(event, st)
    # Вызов дошёл и отработал — снимаем его из очереди незаписанного.
    was_queued = _pending_path(event).exists()
    _pending_drop(_pending_path(event))
    journal(event, "mc.ok", tool=tool, detail="из очереди" if was_queued else "")
    # ...и упавшие попытки того же вызова: у повтора другой tool_use_id (15.09.2026).
    retried = _close_retried(event)
    if retried:
        journal(event, "queue.retried", tool=tool, detail=str(retried))
    # ...и отказы валидации, которые этот вызов исправил: аргументы у них другие (25.09.2026).
    fixed = _close_rejected(event)
    if fixed:
        journal(event, "queue.fixed", tool=tool, detail=str(fixed))
    args = event.get("tool_input")
    if tool in ("edit_article", "save_lesson") and isinstance(args, dict) and args.get("verify"):
        # ⚠️ ПОСЛЕ снятия из очереди и в try: сбой обновления карточки не должен оставлять
        # прошедший вызов «незаписанным» — Stop потребовал бы повторить его (ревью 14.09.2026).
        try:
            _refresh_card_after_verify(event, args)
        except Exception:
            journal(event, "verify.error", tool=tool)
    return 0


# --------------------------------------------------------------------- gate
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:ru|com|pro|net|org|local|lan)\b", re.I)
_DOCKER_RE = re.compile(r"docker\s+(?:exec|restart|logs|stop|start)\s+(?:-\w+\s+)*([\w.-]{3,})")


# Клиенты ssh: адресат — первый позиционный аргумент после опций, у scp и rsync — первый
# аргумент вида host:путь. Опции со значением пропускаются вместе со значением.
_SSH_CLIENTS = frozenset({"ssh", "sftp", "plink", "scp", "rsync"})
_SSH_VALUE_OPTS = frozenset(set("bcDEeFIiJLlmOoPpQRSWw") | {"pw", "hostkey", "loghost", "proxycmd"})
_HOSTSPEC_RE = re.compile(r"^(?:[^@\s:/]+@)?(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9][\w.\-]*):")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][\w.\-:]*$")


def _split_words(s):
    """Слова команды с учётом кавычек; незакрытая кавычка — простой разбор по пробелам."""
    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def _ssh_destination(cmd):
    """(адресат, слова удалённой команды) для ssh/sftp/plink/scp/rsync или ("", []).

    ⚠️ Адресат — позиционный аргумент, а не первый `x@y` в строке: `x@y` бывает и внутри
    удалённой команды (`ssh nas "git clone git@github.com:…"`), и тогда целью становился
    чужой хост (ревью 14.09.2026)."""
    tokens = _split_words(str(cmd or ""))
    for i, tok in enumerate(tokens):
        base = re.sub(r"^[$(`]+", "", tok).replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if base.endswith(".exe"):
            base = base[:-4]
        if base not in _SSH_CLIENTS:
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].startswith("-") and len(tokens[j]) > 1:
            j += 2 if tokens[j][1:] in _SSH_VALUE_OPTS else 1
        rest = tokens[j:]
        if base in ("scp", "rsync"):
            for t in rest:
                m = _HOSTSPEC_RE.match(t)
                if m:
                    return m.group(1).strip("[]"), []
            return "", []
        if not rest:
            return "", []
        host = rest[0].rsplit("@", 1)[-1]
        if host.startswith("["):
            host = host.split("]", 1)[0].lstrip("[")
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
        if not _HOSTNAME_RE.match(host):
            return "", []
        return host, [w for t in rest[1:] for w in t.split()]
    return "", []


def _entities_from_command(cmd):
    """Сущности из строки команды: адресат ssh, адрес, домен, имя контейнера.

    ⚠️ САМУ КОМАНДУ В ПОДСКАЗКУ НЕ ОТДАЁМ. Замер по журналу 27.08.2026: из 35
    блокировок 14 подсказок были мусором вида '$sp = "C:\\Users\\…\\AppData…' —
    прежняя версия брала первые 80 символов поля command. Не нашли сущность — отдаём
    пусто: отсутствующая подсказка лучше вредной.

    ⚠️ Адресат ssh — ПЕРВЫМ и не взаимоисключающе с остальным: прежний разбор давал пусто
    на `ssh -p 2222 root@узел` и `plink -batch узел`, а из `ssh admin@адрес` отдавал имя
    пользователя (ревью 14.09.2026)."""
    found = []
    dest, _words = _ssh_destination(cmd)
    if dest:
        found.append(dest)
    m = _DOCKER_RE.search(cmd)
    if m:
        found.append(m.group(1))
    for rx in (_IP_RE, _DOMAIN_RE):
        m = rx.search(cmd)
        if m:
            found.append(m.group(0))
    seen, out = set(), []
    for x in found:
        if x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return ", ".join(out[:3])


# ⚠️ ЧУЖИЕ КОНФИГИ, читаем УЗКО. В ssh-mcp.json лежат приватные ключи, в конфиге Desktop —
# пароль роутера: берём только name/host и MIKROTIK_HOST. Пути вынесены в модуль, чтобы
# тест мог подставить свои.
SSH_MCP_CONFIG = HOME / ".ssh" / "ssh-mcp.json"
DESKTOP_CONFIG = Path(os.environ.get("APPDATA") or HOME) / "Claude" / "claude_desktop_config.json"
_target_map_cache = None


def _target_map():
    """{имя ssh-соединения: адрес} плюс адрес роутера под ключом 'mikrotik'.

    ⚠️ В объектном формате ssh-mcp.json имя — КЛЮЧ записи (так в README пакета), поля name
    внутри может не быть; в списке — поле name (ревью 14.09.2026)."""
    global _target_map_cache
    if _target_map_cache is not None:
        return _target_map_cache
    out = {}
    try:
        data = json.loads(SSH_MCP_CONFIG.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            pairs = [(k, v.get("host")) for k, v in data.items() if isinstance(v, dict)]
        else:
            pairs = [(it.get("name"), it.get("host")) for it in (data or []) if isinstance(it, dict)]
        for name, host in pairs:
            if name and host:
                out[str(name).strip().casefold()] = str(host).strip()
    except Exception:
        pass
    try:
        srv = (json.loads(DESKTOP_CONFIG.read_text(encoding="utf-8")).get("mcpServers")
               or {}).get("mikrotik") or {}
        host = (srv.get("env") or {}).get("MIKROTIK_HOST")
        if host:
            out["mikrotik"] = str(host).strip()
    except Exception:
        pass
    _target_map_cache = out
    return out


def _resolve_targets(tool, hint):
    """«имя, адрес» вместо одного имени: статьи заведены и по имени, и по адресу.

    ⚠️ У mikrotik-инструментов цели в вызове НЕТ вообще (0 из 1178 вызовов в замере
    14.09.2026): роутер один и задан переменной окружения.
    ⚠️ Адрес идёт СРАЗУ за своим именем: дописанный в конец, он отрезался потолком в три
    (ревью 14.09.2026). Потолок — столько берёт сервер (reflexes._texts). ssh-MCP без
    connectionName ходит через соединение default."""
    tool = tool or ""
    tmap = _target_map()
    items = [h.strip() for h in (hint or "").split(",") if h.strip()]
    if not items and tool.startswith("mcp__ssh__") and "default" in tmap:
        items = ["default"]
    out = []
    if tool.startswith("mcp__mikrotik__") and tmap.get("mikrotik"):
        out.append(tmap["mikrotik"])
    for name in items:
        if name not in out:
            out.append(name)
        host = tmap.get(name.casefold())
        if host and host not in out:
            out.append(host)
    return ", ".join(out[:3])


def _target_hint(event):
    ti = event.get("tool_input") or {}
    tool = event.get("tool_name", "") or ""
    hint = ""
    for key in ("host", "hostname", "server", "ip", "address", "device", "name",
                "connectionName", "project", "base"):
        val = ti.get(key)
        if isinstance(val, str) and val.strip():
            hint = val.strip()[:80]
            break
    if not hint:
        # ⚠️ ssh-MCP передаёт connectionName + cmdString, mikrotik — command/params, и по
        # одному полю command цель не извлекалась в 48 из 140 блокировок (замер 13.09.2026):
        # на самом частом инфраинструменте канал «цель» молчал целиком. Поля перебираем до
        # первой НЕПУСТОЙ находки: пустой разбор одного поля не должен прятать соседнее.
        for key in ("command", "cmdString", "params"):
            val = ti.get(key)
            if isinstance(val, str) and val.strip():
                found = _entities_from_command(val)
                if found:
                    hint = found[:80]
                    break
    return _resolve_targets(tool, hint)


# Инструменты живой инфраструктуры — СВОЙ список, а не доверие матчеру клиента.
INFRA_TOOL_RE = re.compile(r"^mcp__(mikrotik|ssh|synology|1c|ftp-[\w-]+)__", re.IGNORECASE)


def cmd_nul_guard(event):
    """PreToolUse на Bash/PowerShell: блок редиректа в зарезервированное имя Windows.

    Порт grep-хука из config.toml на JSON permissionDecision=deny (как cmd_gate).
    28.09.2026: выяснилось, что Bash-инструмент Kimi Code САМ переписывает
    «> nul» → «> /dev/null» до выполнения (проверено: printf с «> nul» внутри
    строкового литерала вывел «> /dev/null») — опасность нейтрализована
    санитайзером клиента, а сработавший ли grep-хук через exit 2 — не
    верифицировано (хук мог получить уже переписанную команду). Этот страж
    оставлен как defense-in-depth на случай, если хук увидит сырой текст.
    """
    tool = event.get("tool_name", "") or ""
    if tool not in ("Bash", "PowerShell") and not tool.endswith("PowerShell"):
        return 0
    cmd = (event.get("tool_input") or {}).get("command") or ""
    m = HEREDOC_RE.search(cmd)
    if m:
        cmd = cmd[:m.start()]
    # Кавычки — это ДАННЫЕ, а не редирект: «echo "пиши > nul вот так"» пишет
    # ПРО nul, а не В него. Тот же принцип, что у heredoc выше.
    cmd = QUOTED_RE.sub("", cmd)
    hit = NUL_REDIRECT_RE.search(cmd)
    if not hit:
        return 0
    journal(event, "nul.block", tool=tool, detail=hit.group(0).strip()[:40])
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "ЗАБЛОКИРОВАНО: редирект в %s — зарезервированное имя Windows. В bash "
            "и PowerShell это НЕ null-устройство, а обычный файл: он мгновенно "
            "вешает Synology Drive на «Обработка 1 файлов... Подготовка...» и не "
            "удаляется обычным Remove-Item/del — нужен префикс длинных путей и "
            "[System.IO.File]::Delete. Случалось трижды. Замени редирект: bash — "
            "> /dev/null 2>&1; PowerShell — > $null, | Out-Null или "
            "-ErrorAction SilentlyContinue."
            % hit.group(0).strip()
        ),
    }})
    return 0


def cmd_gate(event):
    tool = event.get("tool_name", "") or ""
    ti = event.get("tool_input") or {}

    # ⚠️ Матчер клиента ловит лишнее: в журнале обнаружился вызов гейта на
    # TaskOutput, которого нет ни в одном матчере settings.json. Инструмент, не
    # относящийся к живой инфраструктуре, был бы заблокирован ни за что —
    # поэтому право блокировать проверяем ЗДЕСЬ, у себя.
    is_shell = tool in ("Bash", "PowerShell") or tool.endswith("PowerShell")
    if not is_shell and not INFRA_TOOL_RE.match(tool):
        return 0

    if is_shell:
        cmd = ti.get("command") or ""
        m = HEREDOC_RE.search(cmd)
        if m:
            cmd = cmd[:m.start()]
        if not REMOTE_CMD_RE.search(cmd):
            return 0

    st = load_state(event)
    last_read = float(st.get("last_read_ts") or 0)
    if time.time() - last_read <= FRESH_SEC:
        # Работа с живым железом началась — Stop вправе спросить про finish_task.
        if not st.get("did_infra"):
            st["did_infra"] = True
            save_state(event, st)
        journal(event, "gate.pass")
        memo, _known = _target_memo(event)
        if memo:
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                         "additionalContext": memo}})
        return 0

    # Карточка по цели = знание об узле уже доставлено, блокировать незачем: гейт стоит
    # ровно ради этого. Замер 13.09.2026: карточка готова для 70% целей (по IP 91%), а
    # каждый блок стоит лишний круг (медиана 2 вызова, 15 с, 5.1k токенов). Цель, о
    # которой база молчит, — настоящий пробел знаний, там блокировка остаётся.
    # ⚠️ Отметка last_read_ts обязательна: без неё следующая команда к тому же узлу
    # упрётся в гейт снова, и пропуск превратится в одноразовый.
    memo, known = _target_memo(event)
    if known:
        st["last_read_ts"] = time.time()
        st["did_infra"] = True
        save_state(event, st)
        journal(event, "gate.card", detail=_target_hint(event))
        # Текст пуст, когда памятку по этой цели уже показывали в сессии: повторять её
        # незачем, но знание доставлено — пропускаем молча.
        if memo:
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                         "additionalContext": memo}})
        return 0

    blocks = int(st.get("blocks") or 0)
    if blocks >= MAX_BLOCKS:
        # Клапан: база могла быть недоступна. Пропускаем, но говорим об этом.
        st["blocks"] = 0
        save_state(event, st)
        journal(event, "gate.valve", detail="пропуск после %d блокировок" % blocks)
        emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": (
                "Гейт памяти пропускает вызов после нескольких блокировок подряд. "
                "Если memory-compiler недоступен — скажи об этом владельцу прямо, "
                "не выдумывай факты об инфраструктуре по памяти."
            ),
        }})
        return 0

    st["blocks"] = blocks + 1
    save_state(event, st)

    hint = _target_hint(event)
    journal(event, "gate.block", detail=hint)
    # ⚠️ Памятку здесь НЕ запрашиваем: до этой строки доходит только цель, по которой
    # карточки нет (иначе ветка выше уже пропустила бы вызов). Прежний повторный запрос
    # стал мёртвым кодом и стоил лишнего похода на сервер в каждой блокировке.
    hint_line = ("Ищи по сущности: %s" % hint) if hint else ""
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "СТОП: за последние 15 минут ты ни разу не читал базу знаний, а сейчас "
            "лезешь на живую инфраструктуру (%s). Всё про эту инфраструктуру — "
            "адреса, доступы, пароли, что и почему было настроено, чем кончились "
            "прошлые сессии — лежит в memory-compiler.\n"
            "Сделай СНАЧАЛА: mcp__memory-compiler__search(project=all, "
            "query=<имя сущности + доступы/настройка>), затем read_article по "
            "найденному (секреты расшифровываются сами). %s\n"
            "После этого повтори вызов — гейт пропустит. Не заявляй «нет доступа» "
            "и не проси владельца сделать это руками, не заглянув в базу."
            % (tool, hint_line)
        ),
    }})
    return 0


# ---------------------------------------------------------------- freshness
def _tail_audit(max_bytes=None):
    """Хвост аудит-лога. Хукам хватает 300 КБ (последние часы), отчёту нужно
    больше: на окне в две недели усечённый хвост показывал 120 поисков вместо
    всех и тихо занижал бы любую долю. Без KNOWLEDGE_DIR аудита нет — пусто."""
    if AUDIT_LOG is None:
        return []
    limit = AUDIT_TAIL_BYTES if max_bytes is None else max_bytes
    try:
        size = AUDIT_LOG.stat().st_size
    except Exception:
        return []
    try:
        with AUDIT_LOG.open("rb") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()  # выбрасываем обрезанную строку
            data = f.read()
    except Exception:
        return []
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _foreign_writes(seen_ts, project, mine=None):
    if not seen_ts:
        return []
    try:
        seen = datetime.strptime(seen_ts, AUDIT_TS_FMT)
    except Exception:
        return []
    mine = set(mine or [])
    found = []
    for rec in _tail_audit():
        tool = short_tool(rec.get("tool", ""))
        if tool not in WRITE_TOOLS:
            continue
        try:
            ts = datetime.strptime(rec.get("ts", ""), AUDIT_TS_FMT)
        except Exception:
            continue
        # CLOCK_SKEW: часы сервера и машины расходятся, а запись в аудит идёт
        # ПОСЛЕ ответа инструмента — без допуска собственная запись выглядит
        # свежее отметки и попадает в «чужие».
        if (ts - seen).total_seconds() <= CLOCK_SKEW_SEC:
            continue
        args = rec.get("args") or {}
        key = "%s|%s" % (tool, (args.get("topic") or args.get("filename") or "").strip())
        if key in mine:
            continue
        proj = args.get("project") or ""
        if project and proj and proj != project:
            continue
        topic = args.get("topic") or args.get("filename") or args.get("entity") or ""
        found.append((rec.get("ts"), proj, short_tool(rec.get("tool", "")), str(topic)[:110]))
    return found[-6:]


RULE = (
    "Память memory-compiler — не разовый ритуал в начале сессии. "
    "search обязателен ПЕРЕД каждым утверждением о чужой инфраструктуре "
    "(адрес, пароль, версия, что настроено, почему так сделано) и перед выходом "
    "на живое железо — даже если скил memory-autopilot уже вызывался. "
    "Фразы «нет доступа», «сделай сам», «нужен пароль» без предварительного "
    "поиска в базе запрещены: доступы лежат там."
)
# Окно повтора постоянного правила. Условные блоки (чужие записи, незаписанное,
# досланное) этим окном НЕ ограничены — см. _rule_due.
RULE_EVERY_SEC = 30 * 60


def _rule_due(st, now):
    """Пора ли повторить постоянное правило.

    ⚠️ НЕ НА КАЖДОЕ СООБЩЕНИЕ. Замер 20.09.2026 по транскриптам: за 7 дней текст
    вставлен 1296 раз (493 776 символов), и лишь 44 вставки несли что-то сверх
    него — 96,6% повторяли одно и то же, по 5-6 копий на сессию, в длинных
    десятками. Жёсткие механизмы при этом работают сами: гейт не пустил на
    железо 145 раз за 26 дней. Отсчёт как у подсказки про session_note на
    сервере (NOTE_HINT_SEC): первое сообщение сессии — всегда, дальше по окну.
    """
    return now - float(st.get("rule_ts") or 0) > RULE_EVERY_SEC


def cmd_freshness(event):
    st = load_state(event)
    now = time.time()
    project = st.get("project") or ""
    seen = st.get("seen_audit_ts")
    show_rule = _rule_due(st, now)
    if show_rule:
        st["rule_ts"] = now
    if not seen:
        st["seen_audit_ts"] = datetime.now().strftime(AUDIT_TS_FMT)
        save_state(event, st)
        emit({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": RULE,
        }})
        return 0

    foreign = _foreign_writes(seen, project, st.get("mine"))
    # ⚠️ Условные блоки ниже приходят ВСЕГДА, независимо от окна: чужая запись в
    # базу и незаписанный вызов — новости, которые стареют.
    ctx = RULE if show_rule else ""

    # Попутно пробуем дослать зависшее (троттлинг внутри) — очередь не должна
    # ждать конца сессии, если сервер уже вернулся.
    sent, _left, _desc = _flush_queue()
    # Только свои записи: чужую повторит её сессия, а повтор со стороны пишет дубль.
    own, _orphans = _pending_split(event)
    if sent:
        ctx += "\n\nИз локальной очереди досланы в базу записи: %d." % sent
    if own:
        ctx += ("\n\nНЕ ЗАПИСАНО В БАЗУ: %d. Повтори эти вызовы memory-compiler "
                "(содержание сохранено в %s):\n%s"
                % (len(own), PENDING_DIR, "\n".join(_describe(own))))
    if foreign:
        lines = ["[%s] %s/%s: %s" % (ts, proj or "?", tool, topic)
                 for ts, proj, tool, topic in foreign]
        ctx += (
            "\n\nВНИМАНИЕ, КОНТЕКСТ УСТАРЕЛ. Пока ты работал, в базу писала ДРУГАЯ "
            "сессия — значит инфраструктура могла измениться под тобой:\n"
            + "\n".join(lines)
            + "\nЭто новее твоего снимка (" + seen + "). Прежде чем объяснять "
            "расхождения, чинить или делать выводы — перечитай: "
            "get_active_context / search / read_article по перечисленным темам. "
            "Не разбирайся с изменениями с нуля: их, скорее всего, уже описали."
        )
        # Показали — считаем увиденным, чтобы не повторять на каждый промпт.
        st["seen_audit_ts"] = datetime.now().strftime(AUDIT_TS_FMT)
        save_state(event, st)
        journal(event, "fresh.warn", detail="чужих записей: %d" % len(foreign))
    if show_rule or foreign or sent or own:
        save_state(event, st)
    if not ctx.strip():
        return 0                      # сказать нечего — не шуметь пустым блоком
    emit({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": ctx.lstrip("\n"),
    }})
    return 0


# --------------------------------------------------------------------- stop
def _last_assistant_text(transcript_path, max_lines=120):
    try:
        p = Path(transcript_path)
        if not p.exists():
            return ""
    except Exception:
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except Exception:
        return ""
    chunks = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        rtype = rec.get("type")
        if rtype == "user":
            break
        if rtype != "assistant":
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    chunks.append(blk.get("text") or "")
    return "\n".join(chunks)


def _block(reason):
    """Блок хода (Stop). Возвращает код выхода для cmd_stop.

    Профиль kimi: причина текстом в stderr и код 2 — Claude-стилевой JSON
    {"decision": "block"} Kimi не понимает и показал бы сырым блобом, поэтому в
    stdout не пишем ничего. Профиль claude: JSON decision (проверено годами),
    код 0. Байтами UTF-8: консольная кодировка Windows на пути не стоит."""
    if _EMIT_PLAIN:     # kimi
        try:
            buf = getattr(sys.stderr, "buffer", None)
            if buf is not None:
                buf.write((reason + "\n").encode("utf-8", errors="replace"))
                buf.flush()
            else:
                sys.stderr.write(reason + "\n")
                sys.stderr.flush()
        except Exception:
            pass
        return 2
    # Поле decision у Stop в разных версиях лежит по-разному — отдаём оба места
    # в одном объекте, лишнее клиент проигнорирует.
    emit({
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "decision": "block",
            "reason": reason,
        },
    })
    return 0


def cmd_stop(event):
    if event.get("stop_hook_active"):
        return 0
    st = load_state(event)
    text = _last_assistant_text(event.get("transcript_path", ""))
    now = time.time()

    # Незаписанное в базу — первым делом: сначала пробуем дослать сами, потом,
    # если осталось, не отпускаем ход. «Не ответил компилер» не повод забить.
    sent, _left, _desc = _flush_queue(force=True)
    # ⚠️ Останавливаем ТОЛЬКО своими записями. Очередь общая на машину, а Stop велит
    # повторить вызов: 15.09.2026 посторонняя сессия послушно повторила чужой
    # edit_article, и в статье лёг дубль. Брошенные записи показывает SessionStart.
    own, _orphans = _pending_split(event)
    left, desc = len(own), _describe(own)
    # Долбить бесконечно нельзя: если сервер лежит, повтор не поможет, а вечная
    # блокировка хода превращает сигнал в шум — ровно то, чем был безусловный Stop.
    nags = int(st.get("queue_nags") or 0)
    if left and nags < 3:
        st["queue_nags"] = nags + 1
        save_state(event, st)
        journal(event, "stop.queue", detail="в очереди %d" % left)
        return _block(
            "СТОП. В базу не записано: %d вызов(ов)%s. Содержание лежит в локальной "
            "очереди (%s), автодосыл по REST для них не сработал "
            "или не применим. ПОВТОРИ вызов memory-compiler сейчас — после рестарта "
            "контейнера первый вызов почти всегда падает с -32001, второй проходит. "
            "Если сервер лежит совсем — скажи владельцу прямо, что записать не вышло, "
            "и что очередь досылается автоматически.\n%s"
            % (left, (", досланы автоматически: %d" % sent) if sent else "",
               PENDING_DIR, "\n".join(desc))
        )

    if _is_excuse(text):
        last_read = float(st.get("last_read_ts") or 0)
        if now - last_read > FRESH_SEC:
            journal(event, "stop.excuse")
            return _block(
                "СТОП. Ты сказал, что доступа/пароля нет или что это должен сделать "
                "владелец — и при этом не искал в базе знаний. Доступы к его "
                "инфраструктуре лежат в memory-compiler: "
                "search(project=all, query=<сущность> доступы пароль ssh) -> "
                "read_article (секрет расшифруется сам) -> попробуй сам. "
                "Отказывать можно только показав, ЧТО именно проверено."
            )

    # Про finish_task напоминаем только если работа реально была: писали в базу,
    # ходили на железо или заявляли задачу через start_task. Иначе напоминание
    # вылезало бы в каждой сессии, где просто заглянули в базу, — а безусловный
    # блок ровно этим и обесценил прежний Stop-хук.
    if st.get("did_write") or st.get("did_infra") or st.get("started"):
        if not st.get("finished_ts") and not st.get("stop_nagged"):
            st["stop_nagged"] = True
            save_state(event, st)
            journal(event, "stop.finish")
            return _block(
                "СТОП. В этой сессии ты работал с базой знаний, но finish_task не "
                "вызывал. Если нетривиальная задача решена — вызови "
                "memory-compiler:finish_task сейчас (topic, content, project, "
                "session_summary). Если задача ещё не закончена — так и скажи и продолжай."
            )
    return 0


def cmd_session_start(event):
    """SessionStart: прежний блок <ОБЯЗАТЕЛЬНО> плюс отчёт об очереди — сессия
    начинается со знания, что осталось недописанным (в том числе от прошлой)."""
    sent, _left, _desc = _flush_queue(force=True)
    # Своё (сессия продолжена) и брошенное другими. Свежую запись живой чужой сессии не
    # показываем: её повторит владелец, а повтор отсюда дал бы дубль.
    own, orphans = _pending_split(event)
    ctx = (
        "<ОБЯЗАТЕЛЬНО>АКТИВНА БАЗА ЗНАНИЙ memory-compiler. Прежде чем действовать "
        "над ЛЮБОЙ нетривиальной задачей (код, деплой, баг, настройка, вопрос, факт) "
        "— ОБЯЗАТЕЛЬНО вызови скил memory-autopilot через Skill tool. Он сам "
        "определит проект, дёрнет start_task, подгрузит контекст из памяти, а в "
        "конце finish_task. Единственное исключение — приветствие без задачи. "
        "Это не опционально.</ОБЯЗАТЕЛЬНО>"
    )
    if sent:
        ctx += "\nИз локальной очереди досланы в базу записи: %d." % sent
    if own:
        ctx += ("\nОСТАЛОСЬ НЕЗАПИСАННЫМ: %d. Повтори эти вызовы memory-compiler, "
                "содержание лежит в %s:\n%s"
                % (len(own), PENDING_DIR, "\n".join(_describe(own))))
    if orphans:
        ctx += ("\nБРОШЕНО ДРУГИМИ СЕССИЯМИ (старше %d мин): %d. Содержание лежит в "
                "%s. Прежде чем повторять, прочитай статью: запись "
                "могла уже лечь, и повтор даст дубль:\n%s"
                % (ORPHAN_MIN_AGE // 60, len(orphans), PENDING_DIR,
                   "\n".join(_describe(orphans))))
    emit({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }})
    return 0


# -------------------------------------------------------------------- stats
def _read_journal(hours):
    since = time.time() - hours * 3600
    out = []
    try:
        lines = HOOK_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return out
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
            ts = datetime.strptime(rec.get("ts", ""), AUDIT_TS_FMT)
        except Exception:
            continue
        if ts.timestamp() >= since:
            rec["_ts"] = ts
            out.append(rec)
    return out


def _gate_effect(recs):
    """Сколько блокировок гейта реально привели в базу.

    Это единственная метрика, отвечающая на «работает ли он»: счётчик блокировок
    сам по себе не отличает пользу от помехи. Считаем блокировку сработавшей,
    если в ТОЙ ЖЕ сессии в пределах 10 минут после неё было чтение базы."""
    reads = {}
    for r in recs:
        if r.get("action") == "mc.ok" and r.get("tool") in READ_TOOLS:
            reads.setdefault(r.get("session"), []).append(r["_ts"])
    worked = total = 0
    for r in recs:
        if r.get("action") != "gate.block":
            continue
        total += 1
        for t in reads.get(r.get("session"), []):
            if 0 <= (t - r["_ts"]).total_seconds() <= 600:
                worked += 1
                break
    return worked, total


# Ниже какого размера ответа поиск считаем промахом — только для записей аудита
# без _count (до v1.91.0 и поисковые инструменты, кроме search). Замер по боевому
# аудиту (1130 поисков): медиана 7953 символа, p10 = 910, а у пустых выдач — 29..56.
# Порог 200 берёт именно «ничего не нашлось», не задевая короткие ответы.
SEARCH_MISS_SIZE = 200
# Окно, в котором чтение статьи считается следствием поиска.
SEARCH_FOLLOW_SEC = 180

SEARCH_TOOLS = {"search", "search_by_tag", "search_error", "search_decisions",
                "search_snippets", "ask"}


def _product_stats(hours):
    """Метрики САМОЙ базы, а не сторожа: где память не сработала как продукт.

    Источник — knowledge/_audit.log (локальное зеркало). Сопоставление
    «поиск -> чтение» идёт по времени и глобально: session_id аудит не пишет,
    поэтому при параллельных сессиях цифра приблизительная. Для «часто ли
    выдача вообще пригождается» этого достаточно, для точных выводов есть
    retrieval_eval.py на golden-наборе."""
    since = datetime.now().timestamp() - hours * 3600
    rows = []
    for r in _tail_audit(max_bytes=20_000_000):
        try:
            ts = datetime.strptime(r.get("ts", ""), AUDIT_TS_FMT)
        except Exception:
            continue
        if ts.timestamp() >= since:
            r["_ts"] = ts
            rows.append(r)
    rows.sort(key=lambda r: r["_ts"])

    searches = [r for r in rows if r.get("tool") in SEARCH_TOOLS]
    # Полезным исходом считается ДЕЙСТВИЕ по выдаче — чтение статьи ИЛИ запись в
    # базу. Прежний счёт только по read_article объявлял провалом самый частый
    # удачный случай: ответ нашёлся прямо в превью (выдача поиска весит 13 КБ),
    # и сессия пошла писать. Замер 26.08.2026, 525 поисков за месяц: чтение 51%,
    # запись 17%, ещё поиск 22%, ничего 9%.
    # ⚠️ Исход определяет ПЕРВОЕ событие после поиска, а не наличие действия в
    # окне: `any(...)` засчитывал одно чтение сразу нескольким поискам. Сверка на
    # боевом логе (525 поисков за месяц): по окну 85% полезных, по первому
    # событию — 68%; завышение на 88 поисков.
    pos = {id(r): i for i, r in enumerate(rows)}
    misses, acted, chained = [], 0, 0
    for s in searches:
        # v1.91.0 убрал «score: »/«secret:false» из выдачи search — короткий, но
        # непустой ответ (один результат) стал попадать под SEARCH_MISS_SIZE по одной
        # длине. Если сервер записал число найденного (_count), промах — это
        # count == 0; старые записи без _count остаются на прежнем критерии.
        # ⚠️ Та же логика — analytics.quality() сервера: правка симметрична.
        count = (s.get("args") or {}).get("_count")
        if count is not None:
            is_miss = int(count) == 0
        else:
            is_miss = int(s.get("size") or 0) < SEARCH_MISS_SIZE
        if is_miss:
            misses.append(s)
        for nxt in rows[pos[id(s)] + 1:]:
            if (nxt["_ts"] - s["_ts"]).total_seconds() > SEARCH_FOLLOW_SEC:
                break
            tool = nxt.get("tool", "")
            if tool == "read_article" or short_tool(tool) in WRITE_TOOLS:
                acted += 1
                break
            if tool in SEARCH_TOOLS:
                chained += 1
                break

    # Кто съедает контекст: без этой строки приоритеты ставились вслепую.
    volume = {}
    for r in rows:
        size = r.get("size")
        if isinstance(size, int) and size > 0:
            t = r.get("tool") or "?"
            volume[t] = volume.get(t, 0) + size

    written = [r for r in rows if short_tool(r.get("tool", "")) in WRITE_TOOLS]
    projects = {}
    for r in written:
        proj = (r.get("args") or {}).get("project") or "?"
        projects[proj] = projects.get(proj, 0) + 1
    return {
        "searches": len(searches), "misses": misses, "acted": acted,
        "chained": chained, "written": len(written),
        "volume": sorted(volume.items(), key=lambda kv: -kv[1])[:5],
        "volume_total": sum(volume.values()),
        "projects": sorted(projects.items(), key=lambda kv: -kv[1])[:6],
        "rows": len(rows),
    }


def cmd_stats(event):
    try:
        hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
    except Exception:
        hours = 24.0
    recs = _read_journal(hours)
    if not recs:
        print("Журнал пуст за последние %g ч (%s)" % (hours, HOOK_LOG))
        return 0

    cnt = {}
    for r in recs:
        cnt[r.get("action", "?")] = cnt.get(r.get("action", "?"), 0) + 1
    sessions = sorted({r.get("session") for r in recs if r.get("session")})
    worked, blocked = _gate_effect(recs)
    pending = _pending_all()

    reads = sum(1 for r in recs if r.get("action") == "mc.ok" and r.get("tool") in READ_TOOLS)
    writes = sum(1 for r in recs if r.get("action") == "mc.ok" and r.get("tool") in WRITE_TOOLS)

    print("СТАТИСТИКА СТОРОЖА ПАМЯТИ за %g ч  (сессий: %d)" % (hours, len(sessions)))
    print("-" * 62)
    print("Гейт живой инфраструктуры")
    print("  заблокировано вызовов      %d" % cnt.get("gate.block", 0))
    print("  из них привели в базу      %d%s" % (
        worked, (" (%d%%)" % round(100 * worked / blocked)) if blocked else ""))
    print("  пропущено без помех        %d" % cnt.get("gate.pass", 0))
    print("  сработал клапан            %d" % cnt.get("gate.valve", 0))
    print("Обращения к базе")
    print("  чтений                     %d" % reads)
    print("  записей                    %d" % writes)
    print("  вызовов упало              %d" % cnt.get("mc.fail", 0))
    print("Очередь незаписанного")
    print("  поставлено в очередь       %d" % cnt.get("queue.add", 0))
    print("  дослано по REST            %d" % cnt.get("queue.sent", 0))
    print("  неудачных попыток досыла   %d" % cnt.get("queue.retry", 0))
    print("  висит сейчас               %d" % len(pending))
    print("Предупреждения и блоки хода")
    print("  «контекст устарел»         %d" % cnt.get("fresh.warn", 0))
    print("  стоп на «нет доступа»      %d" % cnt.get("stop.excuse", 0))
    print("  стоп на непустой очереди   %d" % cnt.get("stop.queue", 0))
    print("  напоминание finish_task    %d" % cnt.get("stop.finish", 0))
    r_hit, r_miss = cnt.get("reflex.hit", 0), cnt.get("reflex.miss", 0)
    print("Рефлексы памяти")
    print("  памятка пришла             %d%s" % (
        r_hit, (" (%d%% запросов)" % round(100 * r_hit / (r_hit + r_miss))) if r_hit + r_miss else ""))
    print("  не нашлось                 %d" % r_miss)
    print("  сбой запроса к базе        %d" % cnt.get("reflex.error", 0))

    # ------- то, ради чего это и заводилось: где память подводит как продукт
    ps = _product_stats(hours)
    if ps["rows"]:
        miss_n = len(ps["misses"])
        print("-" * 62)
        print("ПАМЯТЬ КАК ПРОДУКТ (из knowledge/_audit.log, все сессии)")
        print("  поисков                    %d" % ps["searches"])
        if ps["searches"]:
            print("  из них впустую             %d (%d%%)" % (
                miss_n, round(100 * miss_n / ps["searches"])))
            print("  привело к действию         %d (%d%%) — чтение статьи или запись в базу" % (
                ps["acted"], round(100 * ps["acted"] / ps["searches"])))
            print("  за поиском сразу поиск     %d (обычно сбор по разным подтемам)" % ps["chained"])
        if ps.get("volume_total"):
            print("  съедено контекста          %d тыс. символов" % (ps["volume_total"] // 1000))
            for t, v in ps["volume"][:3]:
                print("    %-22s %d тыс." % (t, v // 1000))
        print("  записей в базу             %d" % ps["written"])
        if ps["projects"]:
            print("  активные проекты           " + ", ".join(
                "%s:%d" % (p, n) for p, n in ps["projects"]))
        if ps["misses"]:
            print("  запросы, не давшие НИЧЕГО (кандидаты в статьи или в golden-набор):")
            for r in ps["misses"][-6:]:
                q = (r.get("args") or {}).get("query") or (r.get("args") or {}).get("tag") or ""
                print("    [%s] %s — %s" % (r.get("ts"), r.get("tool"), str(q)[:60]))

    fails = [r for r in recs if r.get("action") in ("mc.fail", "queue.retry")]
    if fails:
        print("-" * 62)
        print("Последние отказы:")
        for r in fails[-5:]:
            print("  [%s] %s %s — %s" % (r.get("ts"), r.get("action"),
                                         r.get("tool", ""), r.get("detail", "")[:70]))
    if pending:
        print("-" * 62)
        print("Не записано в базу прямо сейчас:")
        for line in _describe(pending):
            print("  " + line)

    # ------- выводы: цифра без порога ничего не значит, порог задаём здесь
    verdicts = []
    if blocked and worked / blocked < 0.5:
        verdicts.append(
            "гейт чаще мешает, чем помогает (%d из %d блокировок без похода в базу) "
            "— поднять FRESH_SEC или сузить список инструментов" % (worked, blocked))
    if ps["searches"] >= 10:
        miss_share = len(ps["misses"]) / ps["searches"]
        if miss_share > 0.10:
            verdicts.append(
                "%d%% поисков впустую — это пробел БАЗЫ или ранжирования, "
                "запросы выше стоит завести статьями либо добавить в golden-набор "
                "retrieval_eval" % round(100 * miss_share))
        # ⚠️ Порог по доле действий НАМЕРЕННО НИЗКИЙ. Прежний вердикт срабатывал
        # при <50% и указывал на ранжирование, хотя считал только чтение статьи;
        # на боевых данных к действию ведут 68% поисков. Вердикт о ранжировании
        # имеет смысл лишь при явном провале, а не при норме.
        if ps["acted"] / ps["searches"] < 0.25:
            verdicts.append(
                "лишь %d%% поисков привели к действию — стоит посмотреть ранжирование "
                "на golden-наборе (retrieval_eval)" % round(100 * ps["acted"] / ps["searches"]))
    if cnt.get("mc.fail", 0) >= 3:
        verdicts.append(
            "%d отказов записи — если это -32001, причина в рестартах контейнера "
            "(mc-watcher), лечится не клиентом, а debounce на стороне NAS"
            % cnt.get("mc.fail", 0))
    if len(pending) >= 3:
        verdicts.append("очередь не расходится: проверить доступность %s" % API_URL)
    if verdicts:
        print("-" * 62)
        print("ВЫВОДЫ")
        for v in verdicts:
            print("  • " + v)
    return 0


def cmd_statusline(event):
    """Строка состояния: видно прямо во время работы, без запроса отчёта.

    Держим её ДЕШЁВОЙ — только два маленьких файла. Журнал и аудит здесь не
    читаем: строка перерисовывается постоянно, а на диске у неё мегабайты."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    st = load_state(event)
    parts = []
    last_read = float(st.get("last_read_ts") or 0)
    if last_read:
        mins = int((time.time() - last_read) / 60)
        mark = "!" if (time.time() - last_read) > FRESH_SEC else ""
        parts.append("база %dм назад%s" % (mins, mark))
    else:
        parts.append("база: не читал")
    if st.get("project"):
        parts.append(str(st["project"]))
    n = len(_pending_all())
    if n:
        parts.append("НЕ ЗАПИСАНО: %d" % n)
    if st.get("started") and not st.get("finished_ts"):
        parts.append("задача не закрыта")
    print("[mc] " + " · ".join(parts))
    return 0


# ------------------------------------------------------------------- reflex
# Рефлексы памяти (сервер v1.78.0+): памятка из базы приходит сама — при ошибке
# инструмента (PostToolUseFailure), при чтении файла (PostToolUse на Read) и при
# выходе на цель (внутри gate). Сервер сверяет событие с триггерами статей (раздел
# «## Рефлексы») и отвечает готовым текстом для additionalContext.
REFLEX_TIMEOUT = 1.5
REFLEX_STATE_CAP = 200
REFLEX_DOWN_SEC = 90        # после сбоя столько секунд на сервер не ходим
REFLEX_GATE_TIMEOUT = 0.7   # гейт стоит перед каждой удалённой командой
REFLEX_SKIP_PATH_RE = re.compile(
    r"[\\/]\.(?:claude|kimi-code)[\\/]|[\\/]AppData[\\/]Local[\\/]Temp[\\/]|[\\/]\.git[\\/]",
    re.IGNORECASE)


def _post_json(path, payload, timeout=REFLEX_TIMEOUT):
    """POST JSON в REST базы. None при любом сбое: хук не имеет права ронять работу."""
    import urllib.request
    key = _api_key()
    if not key:
        return None
    req = urllib.request.Request(
        API_URL + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json; charset=utf-8",
                                "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _reflex_state_path(event):
    """Состояние рефлексов — ОТДЕЛЬНЫЙ файл: частые записи на пачку чтений не должны
    трогать файл, по которому гейт решает, читалась ли база."""
    sid = str(event.get("session_id") or "nosession")
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:120]
    return STATE_DIR / (sid + ".reflex.json")


def _reflex_state(event):
    try:
        data = json.loads(_reflex_state_path(event).read_text(encoding="utf-8"))
    except Exception:
        return {}
    # Файл пишет только сам хук, но битый или чужой формат не должен ронять ни одну команду.
    return data if isinstance(data, dict) else {}


def _reflex_state_save(event, st):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(_reflex_state_path(event), json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


def _as_list(value):
    return value if isinstance(value, list) else []


REFLEX_LOCK_WAIT = 1.0      # ждать соседний хук той же сессии; дольше — запись пропускается
REFLEX_LOCK_STALE = 3.0     # замок старше — брошен процессом, убитым посреди записи


def _reflex_lock(path):
    """Межпроцессный замок записи состояния: файл, созданный с O_EXCL. None — замок не взят."""
    lock = path.with_name(path.name + ".lock")
    deadline = time.time() + REFLEX_LOCK_WAIT
    while True:
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return lock
        except FileNotFoundError:
            try:
                lock.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
        except (FileExistsError, PermissionError):
            # PermissionError на Windows — сосед как раз удаляет свой замок.
            # ⚠️ Брошенный замок (процесс убит посреди записи) снимаем АТОМАРНО, переименованием:
            # кто первым переименовал — тот его и снял, остальным os.replace даст ошибку и они
            # пойдут на новый круг. Прежний unlink двое соседей делали одновременно, и второй
            # удалял уже ЧУЖОЙ свежий замок — оба оказывались внутри (ревью 14.09.2026, N4).
            try:
                if time.time() - lock.stat().st_mtime > REFLEX_LOCK_STALE:
                    dead = lock.with_name("%s.dead.%d" % (lock.name, os.getpid()))
                    os.replace(lock, dead)
                    dead.unlink()
            except OSError:
                pass
        except OSError:
            return None
        if time.time() >= deadline:
            return None
        time.sleep(0.002)


def _reflex_state_read(path):
    """Состояние под перезапись: пустое, только если файла нет или он битый.

    ⚠️ Сбой чтения — None, а не пустой снимок: пустой снимок, записанный следом, затирал
    чужое состояние целиком (25 упавших чтений в замере 14.09.2026)."""
    for attempt in range(8):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except ValueError:
            return {}
        except OSError:
            time.sleep(0.005 * (attempt + 1))     # Windows: файл заменяет сосед
    return None


def _reflex_state_update(event, mutate):
    """Поменять состояние рефлексов под замком и по свежему чтению — только своё.

    None — записать не удалось: замок занят дольше REFLEX_LOCK_WAIT или файл не читается.
    ⚠️ Одного перечитывания перед записью мало (так предлагало ревью 14.09.2026, M3). Замер
    того же дня — 8 процессов одной сессии по общему барьеру старта, 10 раундов: без замка
    потеряно 343 из 400 визитов и 266 из 400 цитат карточки. Механизм не только
    read-modify-write: на Windows os.replace падает, пока файл открыт соседом (452 отказа,
    46 записей брошено после всех попыток). Под замком — 0 из 400 и 0 из 400."""
    path = _reflex_state_path(event)
    lock = _reflex_lock(path)
    if lock is None:
        journal(event, "state.busy")
        return None
    try:
        st = _reflex_state_read(path)
        if st is None:
            journal(event, "state.unreadable")
            return None
        mutate(st)
        _reflex_state_save(event, st)
        return st
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _reflex_down_path():
    return STATE_DIR / "_reflex_down.json"


def _reflex_is_down():
    try:
        until = json.loads(_reflex_down_path().read_text(encoding="utf-8")).get("until", 0)
        return time.time() < float(until)
    except Exception:
        return False


def _reflex_down_mark():
    """Общий на все сессии предохранитель: при лежащей NAS каждое событие иначе ждало бы
    таймаут (замер ревью: +1.7 с на событие при закрытом порту)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(_reflex_down_path(), json.dumps({"until": time.time() + REFLEX_DOWN_SEC}))
    except Exception:
        pass


def _reflex_down_clear():
    try:
        _reflex_down_path().unlink()
    except Exception:
        pass


def _reflex_clip(text):
    """Голова и хвост, как режет сервер: трейсбек кончается ошибкой."""
    return text if len(text) <= 4000 else text[:1000] + "\n" + text[-3000:]


_PUNCT_ONLY_RE = re.compile(r"^[\W\d_]+$")


def _reflex_key(kind, text):
    """Ключ «уже спрашивали». У ошибки — до пяти последних содержательных строк: по
    одной последней склеивались разные ошибки (у JSON она «}», у pwsh — общая подсказка)."""
    import hashlib
    if isinstance(text, list):
        text = ",".join(text)
    text = str(text)
    if kind == "error":
        lines = [l.strip() for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("Exit code")
                 and not _PUNCT_ONLY_RE.match(l.strip())]
        text = "\n".join(lines[-5:]) if lines else text
    return kind + ":" + hashlib.sha1(text[:2000].encode("utf-8", "replace")).hexdigest()[:16]


def _reflex_lookup(event, kind, text, timeout=REFLEX_TIMEOUT):
    """Памятки один раз на ключ за сессию; уже показанные статьи исключаются.

    Ключ пишется только после ответа сервера: сбой (рестарт контейнера, таймаут) не
    должен лишать памятки повторяющуюся ошибку. После сбоя — общий предохранитель."""
    if _reflex_is_down():
        return ""
    if kind == "error":
        text = _reflex_clip(text)
    st = _reflex_state(event)
    key = _reflex_key(kind, text)
    if key in _as_list(st.get("keys")):
        return ""
    data = _post_json("/api/reflex", {"kind": kind, "text": text,
                                      "cwd": event.get("cwd") or "",
                                      "exclude": _as_list(st.get("shown"))}, timeout=timeout)
    if not isinstance(data, dict):
        _reflex_down_mark()
        journal(event, "reflex.error", detail=kind)
        return ""
    memos = [m for m in _as_list(data.get("memos")) if isinstance(m, dict)]
    hit = bool(memos and data.get("text"))

    def _remember(st):
        st["keys"] = (_as_list(st.get("keys")) + [key])[-REFLEX_STATE_CAP:]
        if not hit:
            return
        st["shown"] = (_as_list(st.get("shown")) + [
            "%s/%s" % (m.get("project"), m.get("file")) for m in memos])[-REFLEX_STATE_CAP:]
        if kind != "target":
            return
        # Цитаты карточки нужны потом, когда команда отработает: сверять будет cmd_probe.
        # ⚠️ Храним ЧЕТВЁРКУ — статья, чья это цитата, обязательна для вердикта
        # verified/stale, без неё сервер отвечает 400. Ключ — та же нормализация, по
        # которой cmd_probe потом читает; разойдись они, verified не случится никогда.
        # ⚠️ Пары ложатся под слоты ЦЕЛИ-ИСТОЧНИКА — m["targets"] от сервера (v1.80.0):
        # это те из запрошенных целей, по которым статья реально найдена. Раньше пары ВСЕХ
        # статей карточки размазывались по ВСЕМ запрошенным целям, и цитата статьи про узел A
        # ложилась под узел B: команда к B давала ложный stale на статье A, которую никто не
        # исполнял (ревью 14.09.2026, N1 Critical).
        # ⚠️ Повторная карточка той же цели приходит без уже показанных статей (exclude), и
        # замена списка стирала их цитаты. Статья, пришедшая в карточке, заменяет СВОИ пары
        # целиком — так уходят и устаревшие.
        # ⚠️ ИЗВЕСТНОЕ СЛЕДСТВИЕ exclude (M7, не регрессия): статья про ДВА узла, показанная
        # карточкой первого, во вторую карточку не попадёт — слот второго узла останется пуст,
        # и его команда даст reachable, хотя цитата в базе есть.
        # ⚠️ Статья без targets (сервер старше v1.80.0) в сверку не идёт вовсе: verified по ней
        # не случится, но и ложного вердикта не будет.
        # ⚠️ Слот заводится ТОЛЬКО по цели, которую хук сам и спрашивал. Сервер берёт targets
        # из текстов запроса, так что расхождения быть не должно; но разойдись однажды его
        # нормализация с _probe_norm — слоты перестали бы совпадать МОЛЧА, и verified не
        # случился бы ни разу. Промах виден в журнале (ревью 14.09.2026, M3).
        asked = {_probe_norm(i)[:80] for i in (text if isinstance(text, list) else [text])
                 if isinstance(i, str)}
        store = st.get("verify") if isinstance(st.get("verify"), dict) else {}
        by_slot, arrived_by, missed = {}, {}, []
        for m in memos:
            mine = [[m.get("project"), m.get("file"), v[0], v[1]]
                    for v in _as_list(m.get("verify"))
                    if isinstance(v, (list, tuple)) and len(v) == 2]
            for t in _as_list(m.get("targets")):
                if not isinstance(t, str):
                    continue
                slot = _probe_norm(t)[:80]
                if slot not in asked:
                    missed.append(t[:40])
                    continue
                arrived_by.setdefault(slot, set()).add((m.get("project"), m.get("file")))
                by_slot.setdefault(slot, []).extend(mine)
        if missed:
            journal(event, "reflex.slot_miss", detail=", ".join(missed)[:80])
        for slot, arrived in arrived_by.items():
            kept = [p for p in _as_list(store.get(slot))
                    if isinstance(p, list) and len(p) == 4 and (p[0], p[1]) not in arrived]
            merged = kept + by_slot.get(slot, [])
            if merged:
                store[slot] = merged[-REFLEX_STATE_CAP:]
            else:
                store.pop(slot, None)
        # ⚠️ Потолок и по ЧИСЛУ слотов, как у visits/keys/shown: длинная сессия со многими
        # узлами иначе растила бы файл состояния без предела (ревью 14.09.2026, M4).
        st["verify"] = dict(list(store.items())[-REFLEX_STATE_CAP:])

    # ⚠️ Запись — под замком и по свежему чтению: карточки и вердикты той же сессии пишут
    # файл параллельно (ревью 14.09.2026, M3).
    _reflex_state_update(event, _remember)
    if not hit:
        journal(event, "reflex.miss", detail=kind)
        return ""
    journal(event, "reflex.hit",
            detail="%s: %s" % (kind, ", ".join(str(m.get("file")) for m in memos)))
    return str(data["text"])


def _target_memo(event):
    """Цель выхода на железо — те же сущности, что в подсказке гейта. Таймаут короче:
    гейт стоит перед каждой удалённой командой.

    Возвращает (текст памятки, есть ли по цели знание в базе).

    ⚠️ ЭТО РАЗНЫЕ ВОПРОСЫ, и их нельзя мерить одним пустым текстом. Памятка показывается
    РАЗ на ключ за сессию (дедуп), а «по цели есть знание» верно ВСЁ время. Пока гейт
    судил по тексту, вторая команда к тому же узлу блокировалась, хотя карточку показали
    минуту назад: дедуп молча означал «карточки нет» (живая проверка 13.09.2026)."""
    hint = _target_hint(event)
    items = [h.strip() for h in hint.split(",") if h.strip()] if hint else []
    if not items:
        return "", False
    text = _reflex_lookup(event, "target", items, timeout=REFLEX_GATE_TIMEOUT)
    st = _reflex_state(event)
    known = list(_as_list(st.get("targets")))
    marks = [m for m in (_probe_norm(i)[:80] for i in items) if m]
    if text:
        # ⚠️ Помним САМИ ЦЕЛИ, а не имена показанных статей: статья находится по триггеру
        # или адресу в заголовке и называется как угодно («nas-demo» → secret_nas.md).
        # Сверка «цель входит в имя файла» давала ложное «не знаем» (отладка 13.09.2026).
        fresh = [m for m in marks if m not in known]
        if fresh:
            def _add_targets(st):
                cur = [t for t in _as_list(st.get("targets")) if isinstance(t, str)]
                st["targets"] = (cur + [m for m in fresh if m not in cur])[-REFLEX_STATE_CAP:]

            _reflex_state_update(event, _add_targets)
        return text, True
    # Текста нет: либо дедуп (эту цель уже показывали), либо база про неё молчит.
    return "", any(m in known for m in marks)


def _probe_norm(s):
    """Сравнение целей и имён статей: регистр сложен, повторные пробелы сжаты."""
    return " ".join(str(s or "").split()).casefold()


PROBE_NET_RE = re.compile(r"etimedout|timed out|econnreset|network is unreachable|"
                          r"no route to host|temporary failure", re.IGNORECASE)
PROBE_AUTH_RE = re.compile(r"permission denied|authentication fail|unauthorized|"
                           r"access denied|login failed", re.IGNORECASE)

# Выделенные инструменты чтения не несут строки команды — засчитываем их как команду,
# иначе цитата роутера не станет verified никогда.
PROBE_TOOL_CMD = {
    "mikrotik_get_system_identity": "/system identity print",
    "mikrotik_system_info": "/system resource print",
}


def _cmd_words(s):
    """Слова команды для сверки, в нижнем регистре.

    RouterOS пишут и через слэши, и через пробелы: `/system/identity/print` ≡
    `/system identity print`. Сводится только ПЕРВОЕ слово, начатое со слэша, — поэтому
    `/export` не равен `/interface export`, а аргумент `192.0.2.1/24` не рвётся. Сводим по
    виду команды, а не по инструменту: RouterOS бывает и за ssh-MCP."""
    s = str(s or "").strip()
    if s.startswith("/"):
        head, _sep, rest = s[1:].partition(" ")
        s = "/" + head.replace("/", " ") + ((" " + rest) if rest else "")
    return [w.casefold() for part in _split_words(s) for w in part.split()]


def _strip_sudo(words):
    """`sudo` и его флаги перед командой сверку не меняют: `sudo -n cat …` ≡ `cat …`."""
    if words and words[0] == "sudo":
        i = 1
        while i < len(words) and words[i].startswith("-"):
            i += 1
        return words[i:]
    return words


def _ran_words(event, is_shell):
    """Слова команды, ИСПОЛНЕННОЙ на узле, или [], если её не восстановить.

    У ssh из Bash — только удалённая часть после адресата: локальная обёртка на узле не
    исполнялась. Прочие удалённые оболочки (psexec, WinRM) команду не разбирают — там
    честный итог «узел отвечает»."""
    ti = event.get("tool_input") or {}
    mapped = PROBE_TOOL_CMD.get(short_tool(event.get("tool_name", "") or ""))
    if mapped:
        return _cmd_words(mapped)
    if is_shell:
        cmd = str(ti.get("command") or "")
        m = HEREDOC_RE.search(cmd)
        dest, words = _ssh_destination(cmd[:m.start()] if m else cmd)
        return _cmd_words(" ".join(words)) if dest else []
    # ⚠️ Непустой params (`where`, `=`) фильтрует вывод: команда уже не та, что в цитате, и
    # равенство соврало бы — честный итог «узел отвечает» (ревью 14.09.2026, N2). Проверка
    # стоит для ЛЮБОГО необолочечного инструмента, а не только mikrotik: поле params сегодня
    # есть лишь у него, но правило от инструмента не зависит (M6).
    params = ti.get("params")
    if (isinstance(params, dict) and params) or (isinstance(params, str) and params.strip()):
        return []
    return _cmd_words(ti.get("cmdString") or ti.get("command") or "")


def _cmd_matches(quote, ran):
    """Цитата — ВСЯ исполненная команда (после снятия sudo), слово в слово.

    ⚠️ Не префикс: любой хвост меняет вывод, а вердикт выносится по нему. `docker ps -a`
    показывает и остановленные контейнеры — на цитате `docker ps` это давало ЛОЖНОЕ
    verified ровно на протухшем факте; `docker ps | grep`, `cat /etc/hostname > /tmp/h`
    давали ложный stale (ревью 14.09.2026, N2).
    ⚠️ Не подстрока (ревью 14.09.2026, M1): `cat /etc/hostname` совпадал с `…hostname.bak`,
    `docker exec контейнер cat /etc/hostname` читает имя контейнера, а не узла, а замена
    всех слэшей делала `/export` равным `/interface export`."""
    q = _strip_sudo(_cmd_words(quote))
    return bool(q) and _strip_sudo(ran) == q


# Узел, а не шум: локальные адреса и публичные резолверы визитом не считаются (тот же
# стоп-лист, что reflexes.TARGET_STOP на сервере).
VISIT_STOP = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1",
                        "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9"})


def _visit_node(event, is_shell):
    """(адрес, имена, тип) узла, на который вышла команда, или None.

    ⚠️ Только АДРЕСАТ команды: не аргументы (адрес в add_ip_address, имя в
    set_system_identity) и не цели внутри удалённой команды (curl к 127.0.0.1, ping 8.8.8.8,
    имя контейнера). Иначе подсказка просила цитату про шум (ревью 14.09.2026).
    ⚠️ Один узел — один визит: ключ — адрес из карты целей, имя соединения — алиас. Раньше
    имя и адрес давали два визита, и подсказка приходила дважды про один узел."""
    tool = event.get("tool_name", "") or ""
    ti = event.get("tool_input") or {}
    tmap = _target_map()
    if tool.startswith("mcp__mikrotik__"):
        host = tmap.get("mikrotik")
        return (host, [host], "mikrotik") if host and host not in VISIT_STOP else None
    if is_shell:
        cmd = str(ti.get("command") or "")
        m = HEREDOC_RE.search(cmd)
        name, kind = _ssh_destination(cmd[:m.start()] if m else cmd)[0], "shell"
    elif tool.startswith("mcp__ssh__"):
        name, kind = str(ti.get("connectionName") or "default").strip(), "ssh"
        if name == "default" and "default" not in tmap:
            return None
    else:
        return None
    if not name or name.startswith("-"):
        return None
    address = tmap.get(name.casefold()) or name
    if address.casefold() in VISIT_STOP or name.casefold() in VISIT_STOP:
        return None
    return address, sorted({name, address}), kind


# ─── готовая цитата проверки из увиденного вывода (вариант (а), 25.09.2026) ────────────
# Замер 20.09: напоминание с шаблоном «<команда> => <значение>» после 15.09 ни разу не стало
# цитатой — механизм, требующий СФОРМУЛИРОВАТЬ факт руками, не работает. Хук сам видел
# команду и её вывод, поэтому предлагает готовую пару. Замер 25.09 по 50 напоминаниям:
# цельная read-only команда к узлу — 12 из 449 вызовов, годная пара — примерно у каждого
# десятого напоминания. Отсюда УЗКО: белый список стабильных команд, команда целиком,
# значение одной строкой; всё прочее — прежний шаблон, а не угаданная пара.
# ⚠️ Команда хранится В ИСХОДНОМ РЕГИСТРЕ: модель потом исполняет цитату как есть, и
# `cat /opt/x/version` вместо `…/VERSION` дал бы ложный stale.
READY_SEEN_CAP = 3                       # пар на визит: последние по времени, по одной на команду
READY_VALUE_MAX = 80
_READY_META_RE = re.compile(r"[|;&<>`\n]|\$\(")
_READY_ERROR_RE = re.compile(r"no such file|not found|denied|error|cannot|failed|refused|"
                             r"unauthorized|timed out", re.IGNORECASE)
_READY_IPIFY_RE = re.compile(r"^https?://api\.ipify\.org/?$", re.IGNORECASE)
# ⚠️ У curl — только эти опции (слова уже в нижнем регистре): цитату модель потом ИСПОЛНЯЕТ,
# а `-o файл -w %{remote_ip}` выводит тот же IP и при этом пишет на узел.
_CURL_FLAGS = frozenset({"-s", "--silent", "-ss", "-fs", "-sf", "-fss", "-sfs", "--show-error",
                         "-f", "--fail", "-4", "-6"})
_CURL_VALUE_OPTS = frozenset({"-m", "--max-time", "--connect-timeout", "-x", "--proxy"})


def _curl_ipify_ok(words):
    """curl с безопасными опциями и адресом ipify последним словом."""
    i = 1
    while i < len(words) - 1:
        if words[i] in _CURL_VALUE_OPTS:
            i += 2
        elif words[i] in _CURL_FLAGS:
            i += 1
        else:
            return False
    return i == len(words) - 1


def _response_text(resp):
    """Текст вывода инструмента: stdout у Bash, текстовые блоки у MCP, строка как есть.

    Прерванная команда вывода не даёт — её результат неполный."""
    if isinstance(resp, dict):
        if resp.get("interrupted"):
            return ""
        for key in ("stdout", "text"):
            if key in resp:
                return str(resp.get(key) or "")
        return _response_text(resp.get("content")) if "content" in resp else ""
    if isinstance(resp, list):
        return "\n".join(_response_text(x) for x in resp)
    return str(resp or "")


def _ready_kind(words):
    """Вид стабильной read-only команды по её словам (sudo снят) или None.

    ⚠️ Каждая команда списка обязана проходить reflexes.verification_problem СЕРВЕРА — иначе
    готовую пару отобьют уже при записи. `hostname` и `uname -r` сервер отвергает (в них нет
    читающего глагола из VERIFY_VERBS), поэтому их здесь нет: имя узла даёт cat /etc/hostname."""
    if words == ["cat", "/etc/hostname"]:
        return "line"
    if len(words) == 2 and words[0] == "cat":
        if words[1] == "/etc/os-release":
            return "os-release"
        if words[1].rstrip("/").rsplit("/", 1)[-1] == "version":
            return "line"
    if (words[:1] == ["curl"] and len(words) > 1 and _READY_IPIFY_RE.match(words[-1])
            and _curl_ipify_ok(words)):
        return "ip"
    if words == ["/system", "identity", "print"]:
        return "identity"
    if words == ["/system", "resource", "print"]:
        return "resource"
    return None


def _json_field(text, key):
    """Поле key из JSON-ответа инструмента (объект или список объектов) или None."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, list) and data:
        data = data[0]
    return str(data.get(key)) if isinstance(data, dict) and data.get(key) is not None else None


def _kv_line(text, key):
    """Значение строки «key: value» (RouterOS print) или None."""
    m = re.search(r"^\s*%s:\s*(.+?)\s*$" % re.escape(key), text, re.MULTILINE)
    return m.group(1) if m else None


def _ready_value(kind, text):
    """Значение для цитаты из вывода команды данного вида или None."""
    text = text.strip()        # многострочный вывод отсеет _ready_value_ok: перевод строки — служебный
    if kind in ("line", "ip"):
        if not text:
            return None
        if kind == "ip":
            try:
                ipaddress.ip_address(text)
            except ValueError:
                return None
        return text
    if kind == "os-release":
        m = re.search(r"^PRETTY_NAME=(.+)$", text, re.MULTILINE)
        return m.group(1).strip().strip("\"'") if m else None
    key = "name" if kind == "identity" else "version"
    return _json_field(text, key) or _kv_line(text, key) if text else None


def _ready_value_ok(value):
    """Годится ли значение в цитату: одна короткая строка без служебных символов и без
    текста ошибки (сообщение «Permission denied» — не факт об узле)."""
    if not value or not 3 <= len(value) <= READY_VALUE_MAX:   # сервер берёт от 3 символов
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    return not (_READY_ERROR_RE.search(value) or PROBE_AUTH_RE.search(value))


def _ready_pair(event, is_shell):
    """(команда, значение) для готовой цитаты проверки из этого вызова или None.

    Команда — ЦЕЛИКОМ та, что исполнена на узле (как в _ran_words): только тогда её повтор
    даст verified. Перенаправление, конвейер, цепочка — не цитата."""
    if event.get("error") or event.get("hook_event_name") == "PostToolUseFailure":
        return None
    ti = event.get("tool_input")
    if not isinstance(ti, dict):
        return None
    mapped = PROBE_TOOL_CMD.get(short_tool(event.get("tool_name", "") or ""))
    if mapped:
        command = mapped
    elif is_shell:
        cmd = str(ti.get("command") or "")
        if HEREDOC_RE.search(cmd):
            return None
        dest, words = _ssh_destination(cmd)
        command = " ".join(words) if dest else ""
    else:
        params = ti.get("params")
        if (isinstance(params, dict) and params) or (isinstance(params, str) and params.strip()):
            return None
        command = str(ti.get("cmdString") or ti.get("command") or "").strip()
    if not command or _READY_META_RE.search(command):
        return None
    kind = _ready_kind(_strip_sudo(_cmd_words(command)))
    if not kind:
        return None
    value = _ready_value(kind, _response_text(_tool_response(event)))
    return (command, value) if _ready_value_ok(value) else None


def _visit_seen(info):
    """Пары визита [команда, значение, ts] — только целые записи (состояние бывает битым)."""
    seen = info.get("seen") if isinstance(info, dict) else None
    return [p for p in (seen if isinstance(seen, list) else [])
            if isinstance(p, list) and len(p) == 3 and isinstance(p[0], str)
            and isinstance(p[1], str) and isinstance(p[2], (int, float))]


def cmd_probe(event):
    """PostToolUse на инфраинструментах: вердикт живой проверки факта.

    ⚠️ Сравнение с ожидаемым делает КЛИЕНТ: вывод боевого узла — недоверенный ввод,
    на сервер уходит только вердикт (reachable | verified | stale)."""
    tool = event.get("tool_name", "") or ""
    ti = event.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0
    # Право слать вердикт проверяем У СЕБЯ, как в гейте: матчер клиента ловит лишнее,
    # а локальная команда с адресом в аргументах — не выход на узел.
    is_shell = tool in ("Bash", "PowerShell") or tool.endswith("PowerShell")
    if is_shell:
        cmd = str(ti.get("command") or "")
        m = HEREDOC_RE.search(cmd)
        if m:
            cmd = cmd[:m.start()]
        if not REMOTE_CMD_RE.search(cmd):
            return 0
    elif not INFRA_TOOL_RE.match(tool):
        return 0
    node = _visit_node(event, is_shell)
    if node:
        # ⚠️ ВЕРДИКТ — только по адресату команды (имена и адрес узла). Сущности из ТЕЛА
        # команды (`curl` к чужому адресу внутри `ssh nas …`) — не тот узел, на котором
        # исполнялась цитата: их статьи не должны ни получать штамп, ни решать судьбу факта
        # адресата (ревью 14.09.2026, N1). Карточку по ним гейт запрашивает отдельно.
        targets = node[1]
    else:
        targets = [t.strip() for t in (_target_hint(event) or "").split(",") if t.strip()]
    if not targets:
        return 0
    out = str(_tool_response(event) or "")
    err = _error_text(event)
    if PROBE_NET_RE.search(err) or PROBE_NET_RE.search(out):
        return 0                          # узел недоступен — факт в этом не виноват
    if node:
        address, names, kind = node
        pair = _ready_pair(event, is_shell)

        def _mark_visit(st):
            # Визит нужен подсказке: просить цитату имеет смысл только про узел, на котором
            # модель только что работала и видела вывод. Ключ — адрес, имена — алиасы узла.
            visits = st.get("visits") if isinstance(st.get("visits"), dict) else {}
            now = time.time()
            # обновлённый узел уходит в конец очереди, увиденные пары едут вместе с ним
            seen = _visit_seen(visits.pop(address, None))
            if pair:
                seen = [p for p in seen if p[0] != pair[0]] + [[pair[0], pair[1], now]]
            visits[address] = {"ts": now, "kind": kind, "names": names}
            if seen:
                visits[address]["seen"] = seen[-READY_SEEN_CAP:]
            st["visits"] = dict(list(visits.items())[-REFLEX_STATE_CAP:])

        st = _reflex_state_update(event, _mark_visit) or _reflex_state(event)
    else:
        st = _reflex_state(event)
    # Пары адресата: карточка кладёт их под слот каждого имени и адреса узла, поэтому
    # берём по всем именам адресата — цитата найдётся и при выходе по имени, и по адресу.
    # ⚠️ БЕЗ АДРЕСАТА ВЕРДИКТА НЕТ. Если узел не распознан (у mikrotik нет ключа в карте:
    # конфиг Desktop не прочитан, переименован, сменился путь), цели берутся из аргументов и
    # тела команды — обосновать ими утверждение о КОНКРЕТНОМ факте нечем. Живой случай ревью:
    # `/ping 192.0.2.50`, исполненный НА РОУТЕРЕ, ставил verified статье про 192.0.2.50.
    # Остаётся честное «узел отвечает» (ревью 14.09.2026, I1).
    store = st.get("verify") if (node and isinstance(st.get("verify"), dict)) else {}
    pairs = []
    for t in targets:
        for p in _as_list(store.get(_probe_norm(t)[:80])):
            if isinstance(p, list) and len(p) == 4 and p not in pairs:
                pairs.append(p)
    # Отказ доступа — это ОТВЕТ узла, а не молчание: узел жив, а вот факт, чью цитату
    # исполняли, протух. Без такой привязки остаётся честное «узел отвечает».
    auth = bool(PROBE_AUTH_RE.search(err) or PROBE_AUTH_RE.search(out))
    ran = _ran_words(event, is_shell)
    # ⚠️ ОДИН ВЕРДИКТ НА КОМАНДУ (break ниже): если у узла две статьи с одинаковой цитатой,
    # штамп получит только первая — на вторую придёт следующая такая же команда (M2).
    # ⚠️ ЦЕНА ПОЛНОГО РАВЕНСТВА (M5): подтверждается только дословно исполненная цитата.
    # Перенаправление, конвейер, цепочка, префикс переменной, абсолютный путь к бинарю,
    # хвост RouterOS (`print detail`) — всё это даёт reachable, а не verified. Направление
    # безопасное: непонятая команда молчит, а не выдаёт штамп.
    level, hit, hit_command = "reachable", None, None
    for project, file, command, expect in pairs:
        if project and file and _cmd_matches(command, ran):
            # Проект — в нижнем регистре, как на MCP-пути сервера: иначе штамп ляжет на
            # фантомный ключ «Infra/файл» мимо настоящей статьи (ревью 14.09.2026).
            hit = (str(project).strip().lower(), str(file))
            hit_command = command
            level = "verified" if (not auth and _probe_norm(expect) in _probe_norm(out)) else "stale"
            break
    # ⚠️ Ключ статьи обязателен для verified/stale: они относятся к КОНКРЕТНОМУ факту. Без
    # него сервер проштамповал бы и статьи, попавшие по адресу в заголовке (секреты с
    # доступами), чью команду никто не исполнял (ревью 13.09.2026). Для reachable ключ не
    # нужен: «узел жив» верно для всех статей по цели. Цель уходит списком — ручка
    # принимает его с v1.80.0, хук выкатывается строго после деплоя сервера.
    payload = {"target": targets, "level": level}
    if hit:
        payload["project"], payload["file"] = hit
        # ⚠️ КОМАНДА ИСПОЛНЕННОЙ ЦИТАТЫ (v1.82.0): сервер держит вердикт ПО КАЖДОЙ цитате
        # (config.probe_stamp → checks), а не одним слотом на статью — иначе verified по
        # одной цитате затирал stale по другой. Шлём как есть, пробелы схлопнет сервер
        # (probe_command_key). Только при hit: у reachable цитаты нет. Длину держим в 1..300,
        # как проверяет ручка, иначе она вернёт 400 и вердикт потеряется.
        cmd_str = str(hit_command).strip() if hit_command else ""
        if 1 <= len(cmd_str) <= 300:
            payload["command"] = cmd_str
    answer = _post_json("/api/probe", payload, REFLEX_GATE_TIMEOUT)
    # ⚠️ Отбитый или потерянный вердикт виден в журнале: иначе probe.verified считал бы и
    # то, что до сервера не дошло (ревью 14.09.2026). Успешный ответ ручки — объект.
    if isinstance(answer, dict):
        journal(event, "probe." + level, detail=", ".join(targets)[:80])
    else:
        journal(event, "probe.error", detail=("%s: %s" % (level, ", ".join(targets)))[:80])
    return 0


def cmd_reflex(event):
    """PostToolUseFailure (ошибка) и PostToolUse на Read (файл)."""
    name = event.get("hook_event_name") or ""
    tool = event.get("tool_name") or ""
    if tool.startswith("mcp__memory-compiler__"):
        return 0                          # свои отказы разбирает cmd_fail
    if name == "PostToolUseFailure":
        err = _error_text(event)
        if event.get("is_interrupt") or not err.strip():
            return 0
        text = _reflex_lookup(event, "error", err)
    elif name == "PostToolUse" and tool == "Read":
        path = (event.get("tool_input") or {}).get("file_path")
        if not isinstance(path, str) or not path or REFLEX_SKIP_PATH_RE.search(path):
            return 0
        text = _reflex_lookup(event, "file", path)
    else:
        return 0
    if text:
        emit({"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}})
    return 0


# ------------------------------------------------------------- session_arg
# Служебный аргумент, который сервер memory-compiler (v1.76.0+) вынимает из
# вызова и по нему считает свежесть контекста. Сервер старше 1.76.0 упадёт на
# лишнем kwarg. Через мост Claude Desktop аргумент доезжает, только если объявлен
# в схеме инструмента (сервер v1.77.0+), а схемы Desktop кэширует до полного
# перезапуска. Matcher в settings.json включать ТОЛЬКО после деплоя и перезапуска.
CLIENT_SESSION_ARG = "_client_session"
SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def cmd_session_arg(event):
    """PreToolUse на всех mcp__memory-compiler__*: передать серверу id чата.

    Замер 11.09.2026: Claude Desktop отдаёт чатам Code серверы из
    claude_desktop_config.json через свой мост (mcp-remote), и у всех таких
    чатов одна MCP-сессия. Сервер считал свежесть по ней и склеивал чаты:
    новый чат на первом касании получал «25 минут без записи» от чужой работы.
    Заголовки и _meta через мост не доезжают, аргументы — только объявленные.

    updatedInput claude.exe применяет и без permissionDecision. Значение,
    отличное от id чата, перезаписывается: поле видно модели в схеме, и она
    может заполнить его сама. В документации Kimi Code updatedInput нет — он,
    скорее всего, игнорируется; поэтому для Kimi дополнительно шлём серверу
    подсказку (отпечаток вызова → id чата, /api/session_hint): сервер сам
    подставит id чата вызову с совпавшим отпечатком.
    """
    sid = event.get("session_id")
    args = event.get("tool_input")
    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid) or not isinstance(args, dict):
        return 0
    if args.get(CLIENT_SESSION_ARG) == sid:
        return 0
    # U+FFFD в аргументах — след порчи кодировки при чтении stdin. Такой ввод не
    # возвращаем: без хука вызов уйдёт как есть, а с хуком уехал бы испорченным.
    if "�" in json.dumps(args, ensure_ascii=False):
        journal(event, "session_arg.skip", detail="U+FFFD во вводе")
        return 0
    # Подсказка серверу (Kimi Code без updatedInput, аудит 28.09.2026): kimi
    # updatedInput не применяет — аргумент _client_session до сервера не доезжает,
    # и чаты склеиваются в одну MCP-сессию. Боковой канал: отпечаток вызова → id
    # чата; call_tool подставит id чата вызову с совпавшим отпечатком. Отпечаток —
    # ДУБЛИКАТ канонизации freshness.call_fingerprint (хук не может импортировать
    # сервер): tool без префикса + исходный tool_input ДО инъекции, с вырезанным
    # _client_session, если модель успела его заполнить. Сбой отправки не меняет
    # поведение хука: updatedInput отдаём в любом случае.
    try:
        raw_args = dict(args)
        raw_args.pop(CLIENT_SESSION_ARG, None)
        fp = hashlib.sha1((short_tool(event.get("tool_name", "")) + "\0" + json.dumps(
            raw_args, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            default=str)).encode("utf-8")).hexdigest()
        _post_json("/api/session_hint", {"fp": fp, "chat_id": sid}, timeout=2)
        journal(event, "session_arg.hint")
    except Exception:
        pass
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": dict(args, **{CLIENT_SESSION_ARG: sid}),
    }})
    # Журнал — единственный след, что хук отработал: сервер аргумент вынимает
    # и в аудит не пишет, а в выдаче его не видно.
    journal(event, "session_arg.add")
    return 0


# ─── подсказка дописать цитату проверки (v1.80.0) ────────────────────────────
# Замер 14.09.2026 отверг подбор цитаты из истории команд: цельная read-only команда к узлу
# перед записью о нём была в 3,3% случаев, годных кандидатов ~5%. Поэтому цитату выбирает
# МОДЕЛЬ — она видела вывод и понимает, какой факт стабилен. Хук лишь напоминает и только
# там, где модель только что работала с узлом.
NUDGE_WINDOW = 7200
NUDGE_TOOLS = {"save_lesson", "finish_task", "edit_article"}
NUDGE_CAP = 2
_NUDGE_TIPS = ("Стабильные факты: Linux по ssh — `cat /etc/hostname`, `cat /etc/os-release`, "
               "`docker ps`; RouterOS — `/system identity print`, `/system resource print`.")


def _token_in(needle, hay):
    """Цель отдельным токеном: «node-demo» не должен ловиться внутри «node-demo2»."""
    # ⚠️ Сначала дешёвое `in`: регулярка по каждому визиту на длинной записи стоила 7.1 с на
    # 200 визитах и тексте 1.6 МБ — выше таймаута хука (ревью 14.09.2026, M4).
    if not needle or needle not in hay:
        return False
    # Точка после цели — конец предложения («роутер 192.0.2.1.»), а точка со словом дальше —
    # другое имя («node-demo.local»): первое совпадение, второе нет.
    return re.search(r"(?<![\w.\-])" + re.escape(needle) + r"(?![\w\-]|\.\w)", hay) is not None


def _visit_names(address, info):
    """Имена узла для поиска в записи: адрес-ключ и алиасы визита (у старых визитов их нет)."""
    raw = [address] + (_as_list(info.get("names")) if isinstance(info, dict) else [])
    out = []
    for name in raw:
        norm = _probe_norm(name) if isinstance(name, str) else ""
        if norm and norm not in out:
            out.append(norm)
    return out


def _visit_age(info, now):
    """Возраст визита в секундах; кривая отметка времени — визит считается старым."""
    try:
        return now - float(info.get("ts") or 0)
    except (AttributeError, TypeError, ValueError):
        return float("inf")


def cmd_nudge(event):
    """PostToolUse на записи в базу: попросить дописать цитату проверки к факту об узле."""
    tool = short_tool(event.get("tool_name", "") or "")
    if tool not in NUDGE_TOOLS:
        return 0
    args = event.get("tool_input")
    if not isinstance(args, dict) or args.get("verify"):
        return 0
    # ⚠️ Только после УСПЕШНОЙ записи, и путь — из строки маркера: первый попавшийся `x/y.md`
    # давал подсказку на «Статья не найдена: …» и путь соседней статьи из того же ответа
    # (ревью 14.09.2026, M2).
    m = _WRITE_OK_RE.search(str(_tool_response(event) or ""))
    if not m:
        return 0
    project, filename = m.group(1).lower(), m.group(2)
    if filename.startswith("secret_"):
        return 0
    text = _probe_norm(" ".join(str(args.get(k) or "") for k in
                                ("topic", "content", "filename", "entity", "session_summary")))
    if not text:
        return 0
    st = _reflex_state(event)
    visits = st.get("visits") if isinstance(st.get("visits"), dict) else {}
    nudged = [n for n in _as_list(st.get("nudged")) if isinstance(n, str)]
    now = time.time()
    # ⚠️ Узел ищется по ВСЕМ своим именам: визит лежит под адресом, а в записи модель пишет и
    # имя соединения (замер 14.09.2026: половина упоминаний узла — только имя).
    hits = []
    for address, info in visits.items():
        if _visit_age(info, now) > NUDGE_WINDOW:
            continue
        names = _visit_names(address, info)
        if any(_token_in(n, text) for n in names):
            hits.append((address, names))
    fresh = [(a, names) for a, names in hits if a not in nudged][:NUDGE_CAP]
    if not fresh:
        if hits:
            journal(event, "nudge.skip", detail="уже просили: %s" % ", ".join(a for a, _n in hits)[:60])
        return 0

    def _remember(st):
        seen = [n for n in _as_list(st.get("nudged")) if isinstance(n, str)]
        st["nudged"] = (seen + [a for a, _n in fresh if a not in seen])[-REFLEX_STATE_CAP:]

    # ⚠️ Запись — под замком и по свежему чтению: снимок из начала команды затирал визиты,
    # которые параллельно пишет probe (ревью 14.09.2026, M3).
    _reflex_state_update(event, _remember)
    journal(event, "nudge.shown", detail=", ".join(a for a, _n in fresh)[:60])
    # Готовая пара — только того узла, что стоит в триггере вызова, и только увиденная в
    # окне напоминания: старый вывод мог уже не соответствовать узлу.
    target = fresh[0][0]
    ready = ["%s => %s" % (cmd, value)
             for cmd, value, ts in sorted(_visit_seen(visits.get(target)), key=lambda p: -p[2])
             if now - ts <= NUDGE_WINDOW][:NUDGE_CAP]
    if ready:
        journal(event, "nudge.ready", detail=("%s: %d" % (target, len(ready)))[:60])
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
            "Память (цитата проверки): запись %s/%s — про узел %s. В этой сессии ты уже видел "
            "вывод, из которого готова цитата проверки. Если факт относится к этой статье и у "
            "неё ещё нет такой цитаты, допиши вызовом ниже — тогда следующий выход на узел "
            "подтвердит факт сам:\n"
            "edit_article(project=\"%s\", filename=\"%s\", triggers=[\"цель: %s\"], verify=%s)"
            % (project, filename, "; ".join(" / ".join(names) for _a, names in fresh),
               project, filename, target, json.dumps(ready, ensure_ascii=False)))}})
        return 0
    # ⚠️ Хук не знает, есть ли у статьи цитата: «цитаты нет» было бы ложью на статье, где
    # она уже есть (ревью 14.09.2026, M2). Триггер — с адресом узла, а не заглушкой.
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
        "Память (цитата проверки): запись %s/%s — про узел %s. Если у статьи ещё нет цитаты "
        "проверки, а в этой сессии ты видел стабильный факт об узле, допиши его цитатой — "
        "тогда следующий выход на узел подтвердит факт сам:\n"
        "edit_article(project=\"%s\", filename=\"%s\", triggers=[\"цель: %s\"], "
        "verify=[\"<команда> => <значение>\"])\n"
        "Значение бери из уже полученного вывода, не выдумывай; пароли не вставляй. %s"
        % (project, filename, "; ".join(" / ".join(names) for _a, names in fresh),
           project, filename, fresh[0][0], _NUDGE_TIPS))}})
    return 0


# ------------------------------------------------------------------- compact
# PostCompact: напоминание про save_compact. Перенесено из inline-echo в конфиге
# клиента: там кириллица регулярно билась в mojibake, а текст дублировался.
COMPACT_TEXT = (
    "КОНТЕКСТ БЫЛ СЖАТ. Сейчас обязательно: (1) Определи проект "
    "(route_project + cwd). (2) Вызови memory-compiler:save_compact(project, "
    "summary=краткое резюме того что было до сжатия — что делали, какие решения, "
    "что осталось). Это сохранит continuous memory. (3) Если задача завершена — "
    "finish_task."
)


def cmd_compact(event):
    emit({"hookSpecificOutput": {
        "hookEventName": "PostCompact",
        "additionalContext": COMPACT_TEXT,
    }})
    return 0


COMMANDS = {
    "mark": cmd_mark, "gate": cmd_gate, "freshness": cmd_freshness, "stop": cmd_stop,
    "intent": cmd_intent, "fail": cmd_fail, "flush": cmd_flush,
    "session_start": cmd_session_start, "stats": cmd_stats, "statusline": cmd_statusline,
    "session_arg": cmd_session_arg, "reflex": cmd_reflex, "probe": cmd_probe,
    "nudge": cmd_nudge, "nul_guard": cmd_nul_guard, "compact": cmd_compact,
}


def main():
    # --client=claude|kimi выписывает генератор конфигов; из разбора убираем,
    # чтобы argv подкоманды (часы у stats) не съезжали. Значение при вырезании
    # запоминаем в env: detect_client() читает argv уже после нашей подмены
    # sys.argv — без запоминания профиль детектился по эвристике и падал в
    # fallback claude (багрепорт 02.10.2026: журнал Kimi Work уходил в
    # ~/.claude/hooks/). Прямое присваивание, не setdefault: argv в приоритете
    # над env.
    argv = []
    for arg in sys.argv[1:]:
        if arg.startswith("--client="):
            os.environ["MC_GUARD_CLIENT"] = arg.split("=", 1)[1]
            continue
        argv.append(arg)
    sys.argv = [sys.argv[0]] + argv
    if not argv or argv[0] not in COMMANDS:
        return 0
    if argv[0] == "stats":
        # Отчёт зовут из терминала: читать stdin нельзя — он ждал бы ввода вечно.
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        _ensure_client({})
        return cmd_stats({}) or 0
    event = read_event()
    _ensure_client(event)
    try:
        return COMMANDS[argv[0]](event) or 0
    except Exception:
        # Хук не имеет права ронять работу: молча пропускаем.
        return 0


if __name__ == "__main__":
    sys.exit(main())
