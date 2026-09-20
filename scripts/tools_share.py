"""Сколько схем инструментов memory-compiler реально попадает в контекст через ToolSearch.

ToolSearch пишет в транскрипт не схемы, а блоки tool_reference; схему по ссылке
подставляет API. Поэтому вес каждой схемы берётся из замера /context (claude.exe
2.1.270, 15.09.2026), а не из текста транскрипта.

Запуск: python tools_share.py <дней>
Цена в эквивалентах входного токена: вход 1, запись кэша 5м 1.25, 1ч 2, чтение 0.1, выход 5.
"""
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]

DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0
PREFIX = "mcp__memory-compiler__"
TOOL_TOKENS = {
    "add_project": 160, "article_history": 195, "ask": 204, "backlinks": 262, "close_question": 307,
    "compile": 507, "consolidate": 280, "context_gaps": 219, "delete_article": 199, "edit_article": 589,
    "finish_task": 572, "gap_report": 284, "get_active_context": 169, "get_context": 190,
    "get_current": 205, "get_project_deps": 152, "get_runbook": 192, "get_summary": 172,
    "git_capture": 501, "import_obsidian": 375, "ingest": 411, "init_schema": 199, "knowledge_gap": 340,
    "lint": 267, "list_projects": 118, "list_templates": 115, "load_session": 165, "open_questions": 194,
    "read_article": 192, "reindex": 127, "remove_project": 242, "route_project": 340, "save_compact": 310,
    "save_contexts": 295, "save_decision": 339, "save_from_template": 274, "save_lesson": 843,
    "save_runbook": 252, "save_secret": 264, "save_session": 329, "save_tracking": 330, "search": 189,
    "search_by_tag": 185, "search_decisions": 170, "search_error": 195, "search_snippets": 205,
    "session_note": 284, "set_project_deps": 227, "stale_facts": 277, "start_task": 252,
}
ALL_TOKENS = sum(TOOL_TOKENS.values())
DEFAULT_TOKENS = int(statistics.median(TOOL_TOKENS.values()))
since = time.time() - DAYS * 86400


def epoch(row):
    stamp = row.get("timestamp")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def usage_parts(message):
    usage = message.get("usage") or {}
    creation = usage.get("cache_creation") or {}
    write_1h = creation.get("ephemeral_1h_input_tokens") or 0
    write_5m = creation.get("ephemeral_5m_input_tokens")
    if write_5m is None:
        write_5m = max((usage.get("cache_creation_input_tokens") or 0) - write_1h, 0)
    return (usage.get("input_tokens") or 0, write_5m, write_1h,
            usage.get("cache_read_input_tokens") or 0, usage.get("output_tokens") or 0)


def weight(name):
    return TOOL_TOKENS.get(name[len(PREFIX):], DEFAULT_TOKENS)


seen_messages, seen_reads, seen_results = set(), set(), set()
totals = [0, 0, 0, 0, 0]
calls = 0
calls_per_session = {}
tools_per_session = {}
write_tokens = 0
read_tokens = 0.0
started = time.time()

for path in (Path.home() / ".claude" / "projects").rglob("*.jsonl"):
    try:
        if path.stat().st_mtime < since:
            continue
    except OSError:
        continue
    loaded = set()
    in_context = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            is_assistant = '"type":"assistant"' in line and '"usage"' in line
            is_reference = '"tool_reference"' in line and PREFIX in line
            if not (is_assistant or is_reference):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            moment = epoch(row)
            session = row.get("sessionId") or str(path)
            if is_reference:
                for block in (row.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    content = block.get("content")
                    if not isinstance(content, list):
                        continue
                    names = {part.get("tool_name") for part in content
                             if isinstance(part, dict) and part.get("type") == "tool_reference"
                             and str(part.get("tool_name", "")).startswith(PREFIX)}
                    fresh = names - loaded
                    loaded |= names
                    in_context += sum(weight(name) for name in fresh)
                    key = block.get("tool_use_id")
                    if fresh and moment and moment >= since and key not in seen_results:
                        seen_results.add(key)
                        write_tokens += sum(weight(name) for name in fresh)
                        tools_per_session.setdefault(session, set()).update(names)
                continue
            message = row.get("message") or {}
            key = message.get("id")
            if not key or moment is None or moment < since:
                continue
            if in_context and not row.get("isSidechain") and key not in seen_reads:
                seen_reads.add(key)
                read_tokens += in_context
            if key in seen_messages:
                continue
            seen_messages.add(key)
            parts = usage_parts(message)
            for index in range(5):
                totals[index] += parts[index]
            calls += 1
            calls_per_session[session] = calls_per_session.get(session, 0) + 1

equiv = totals[0] + 1.25 * totals[1] + 2 * totals[2] + 0.1 * totals[3] + 5 * totals[4]
writes = totals[1] + totals[2]
write_mult = (1.25 * totals[1] + 2 * totals[2]) / writes if writes else 1.25
cost_write = write_tokens * write_mult
cost_read = read_tokens * 0.1
cost = cost_write + cost_read
premise = len(calls_per_session) * ALL_TOKENS * write_mult + calls * ALL_TOKENS * 0.1
counts = sorted(len(v) for v in tools_per_session.values())
per_session_tokens = sorted(sum(weight(n) for n in v) for v in tools_per_session.values())
million = 1_000_000

print(f"окно {DAYS:g} дн., сессий с вызовами {len(calls_per_session)}, вызовов API {calls}, разбор {time.time() - started:.0f} с")
print(f"всего эквивалентов входного токена: {equiv / million:.1f} млн (множитель записи {write_mult:.2f})")
print(f"все 50 схем компилера по /context: {ALL_TOKENS} токенов")
print(f"сессий, где схемы компилера подгружались: {len(tools_per_session)} из {len(calls_per_session)}")
if counts:
    print(f"разных инструментов на такую сессию: медиана {statistics.median(counts):.0f}, максимум {counts[-1]}")
    print(f"токенов схем на такую сессию: медиана {statistics.median(per_session_tokens):.0f}, "
          f"среднее {statistics.mean(per_session_tokens):.0f}, максимум {per_session_tokens[-1]}")
print(f"фактическая стоимость схем компилера: запись {cost_write / million:.2f} млн + чтение {cost_read / million:.2f} млн "
      f"= {cost / million:.2f} млн экв. = {100 * cost / equiv:.2f}% расхода")
print(f"посылка D (все схемы в каждой сессии с первого хода): {premise / million:.2f} млн экв. = {100 * premise / equiv:.2f}% расхода")
