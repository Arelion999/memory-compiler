"""Доля memory-autopilot в расходе Claude Code по транскриптам за N дней.

Запуск: python skill_share.py <дней> <токенов скила сейчас> <токенов после сжатия>
Пример: python skill_share.py 7 10106 4000
Цена в эквивалентах входного токена: вход 1, запись кэша 5м 1.25, 1ч 2, чтение 0.1, выход 5.
Вызовы API дедуплицируются по message.id (возобновлённые сессии копируют историю между файлами).
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]

DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0
SKILL_TOKENS = float(sys.argv[2]) if len(sys.argv) > 2 else 10106.0
TARGET_TOKENS = float(sys.argv[3]) if len(sys.argv) > 3 else 4000.0
LOAD_MARK = "Base directory for this skill:"
SKILL_MARK = "skills\\\\memory-autopilot"

since = time.time() - DAYS * 86400


def epoch(row):
    stamp = row.get("timestamp")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


seen_messages = set()
seen_loads = set()
seen_skill_calls = set()
totals = dict(input=0, write_5m=0, write_1h=0, read=0, output=0)
calls = loads = skill_calls = files = 0
started = time.time()

for path in (Path.home() / ".claude" / "projects").rglob("*.jsonl"):
    try:
        if path.stat().st_mtime < since:
            continue
    except OSError:
        continue
    files += 1
    loaded = False
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            is_load = LOAD_MARK in line and SKILL_MARK in line and '"type":"user"' in line
            is_assistant = '"type":"assistant"' in line and '"usage"' in line
            if not (is_load or is_assistant):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            moment = epoch(row)
            if is_load:
                loaded = True
                key = row.get("uuid")
                if moment and moment >= since and key not in seen_loads:
                    seen_loads.add(key)
                    loads += 1
                continue
            message = row.get("message") or {}
            key = message.get("id")
            if not key or moment is None or moment < since:
                continue
            if loaded and not row.get("isSidechain") and key not in seen_skill_calls:
                seen_skill_calls.add(key)
                skill_calls += 1
            if key in seen_messages:
                continue
            seen_messages.add(key)
            usage = message.get("usage") or {}
            creation = usage.get("cache_creation") or {}
            write_1h = creation.get("ephemeral_1h_input_tokens") or 0
            write_5m = creation.get("ephemeral_5m_input_tokens")
            if write_5m is None:
                write_5m = max((usage.get("cache_creation_input_tokens") or 0) - write_1h, 0)
            totals["input"] += usage.get("input_tokens") or 0
            totals["write_5m"] += write_5m
            totals["write_1h"] += write_1h
            totals["read"] += usage.get("cache_read_input_tokens") or 0
            totals["output"] += usage.get("output_tokens") or 0
            calls += 1

equiv = (totals["input"] + 1.25 * totals["write_5m"] + 2 * totals["write_1h"]
         + 0.1 * totals["read"] + 5 * totals["output"])
writes = totals["write_5m"] + totals["write_1h"]
write_mult = (1.25 * totals["write_5m"] + 2 * totals["write_1h"]) / writes if writes else 1.25
delta = SKILL_TOKENS - TARGET_TOKENS
skill_now = loads * SKILL_TOKENS * write_mult + skill_calls * SKILL_TOKENS * 0.1
saving = loads * delta * write_mult + skill_calls * delta * 0.1

million = 1_000_000
print(f"окно {DAYS:g} дн., файлов {files}, вызовов API {calls}, разбор {time.time() - started:.0f} с")
print("токены, млн: вход {:.1f}; запись кэша 5м {:.1f}, 1ч {:.1f}; чтение кэша {:.1f}; выход {:.1f}".format(
    totals["input"] / million, totals["write_5m"] / million, totals["write_1h"] / million,
    totals["read"] / million, totals["output"] / million))
print(f"всего эквивалентов входного токена: {equiv / million:.1f} млн (средний множитель записи {write_mult:.2f})")
print(f"загрузок memory-autopilot: {loads}; вызовов API с ним в контексте: {skill_calls} "
      f"(в среднем {skill_calls / loads if loads else 0:.0f} на загрузку)")
print(f"скил сейчас ({SKILL_TOKENS:.0f} ток.): {skill_now / million:.2f} млн экв. = {100 * skill_now / equiv:.2f}% расхода")
print(f"экономия при сжатии до {TARGET_TOKENS:.0f} ток.: {saving / million:.2f} млн экв. = {100 * saving / equiv:.2f}% расхода")
