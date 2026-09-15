"""Расход токенов со стороны МОДЕЛИ по транскриптам Claude Code.

Поле size в _audit.log считает то, что сервер отдал, а не то, что модель
получила: у search при объявленном outputSchema клиент показывает
structuredContent и прячет текстовый блок. Транскрипт хранит ровно отданное
модели, поэтому эффект правок выдачи меряется здесь.

Запуск: python scripts/model_side_usage.py [--days 7] [--root DIR] [--prefix mcp__memory-compiler__]
"""
import argparse
import glob
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime

DEFAULT_PREFIX = "mcp__memory-compiler__"
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
              "output_tokens", "cache_5m", "cache_1h")


def _usage(u):
    """Плоская запись usage с разбивкой записи кэша по TTL, если API её прислал."""
    u = u or {}
    split = u.get("cache_creation") or {}
    return {"input_tokens": u.get("input_tokens") or 0,
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens") or 0,
            "cache_read_input_tokens": u.get("cache_read_input_tokens") or 0,
            "output_tokens": u.get("output_tokens") or 0,
            "cache_5m": split.get("ephemeral_5m_input_tokens") or 0,
            "cache_1h": split.get("ephemeral_1h_input_tokens") or 0}


def _cost(u):
    """Эквивалент входного токена по ценам API как прокси расхода лимита.

    Запись кэша на час стоит вдвое дороже входа, на 5 минут — в 1.25 раза; Claude Code
    пишет кэш на час. Без разбивки по TTL считаем по 5-минутной цене (нижняя оценка).
    """
    if u["cache_5m"] or u["cache_1h"]:
        write = u["cache_5m"] * 1.25 + u["cache_1h"] * 2.0
    else:
        write = u["cache_creation_input_tokens"] * 1.25
    return u["input_tokens"] + write + u["cache_read_input_tokens"] * 0.1 + u["output_tokens"] * 5.0


def _ts(o):
    try:
        return datetime.fromisoformat(o["timestamp"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _texts(content):
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [x.get("text", "") or "" for x in content if isinstance(x, dict)]
    return []


def measure(files, since=0.0, prefix=DEFAULT_PREFIX):
    """Сводка по результатам инструментов, как их получила модель.

    Дедуп по tool_use_id и id сообщения: возобновлённая сессия копирует историю
    в новый файл. Реплики сабагентов (isSidechain) не считаются.
    """
    seen_tools, owner = set(), {}
    chars = defaultdict(list)
    search = {"n": 0, "link_chars": 0, "json_chars": 0, "other_chars": 0,
              "followed_by_read": 0, "followed_by_search": 0}
    samples, token_eq = [], 0.0
    for path in files:
        names, inputs, result_chars = {}, {}, {}
        turns, local = [], {}
        prev, keys, read_hit = None, set(), False
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        names[b.get("id")] = b.get("name", "")
                        inputs[b.get("id")] = b.get("input") or {}
                if o.get("isSidechain") or _ts(o) < since:
                    continue
                if o.get("type") == "assistant" and msg.get("id"):
                    mid = msg["id"]
                    if owner.setdefault(mid, path) != path:
                        continue
                    if mid not in local:
                        local[mid] = len(turns)
                        turns.append({"usage": dict.fromkeys(USAGE_KEYS, 0), "tools": []})
                    turn = turns[local[mid]]
                    for k, v in _usage(msg.get("usage")).items():
                        turn["usage"][k] = max(turn["usage"][k], v)
                    turn["tools"] += [b.get("id") for b in content
                                      if isinstance(b, dict) and b.get("type") == "tool_use"]
                    continue
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    tid = b.get("tool_use_id")
                    name = names.get(tid, "")
                    if not name.startswith(prefix) or tid in seen_tools:
                        continue
                    seen_tools.add(tid)
                    tool = name[len(prefix):]
                    blocks = _texts(b.get("content"))
                    size = sum(map(len, blocks))
                    chars[tool].append(size)
                    result_chars[tid] = size
                    if prev == "search" and tool == "search":
                        search["followed_by_search"] += 1
                    if tool == "search":
                        search["n"] += 1
                        keys, read_hit = set(), False
                        for text in blocks:
                            if text.startswith("[Resource link:"):
                                search["link_chars"] += len(text)
                            elif text.lstrip().startswith("{"):
                                search["json_chars"] += len(text)
                                try:
                                    keys = {(r.get("project"), r.get("file"))
                                            for r in json.loads(text).get("results", [])}
                                except ValueError:
                                    pass
                            else:
                                search["other_chars"] += len(text)
                    elif tool == "read_article" and keys and not read_hit:
                        inp = inputs.get(tid, {})
                        if (inp.get("project"), inp.get("filename")) in keys:
                            search["followed_by_read"] += 1
                            read_hit = True
                    prev = tool
        for i, turn in enumerate(turns):
            token_eq += _cost(turn["usage"])
            if i + 1 < len(turns) and len(turn["tools"]) == 1 and result_chars.get(turn["tools"][0], 0) >= 3000:
                nxt = turns[i + 1]["usage"]
                new = (nxt.get("input_tokens", 0) + nxt.get("cache_creation_input_tokens", 0)
                       - turn["usage"].get("output_tokens", 0))
                if new > 0:
                    samples.append(new / result_chars[turn["tools"][0]])
    tools = {t: {"n": len(v), "chars": sum(v), "median": int(statistics.median(v))}
             for t, v in chars.items()}
    return {"tools": tools, "search": search, "token_eq": token_eq,
            "tokens_per_char": statistics.median(samples) if samples else None}


def main(argv=None):
    # Консоль Windows в cp1251 не кодирует «≈» и падает с UnicodeEncodeError
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Расход со стороны модели по транскриптам Claude Code")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    args = ap.parse_args(argv)
    since = time.time() - args.days * 86400
    files = sorted(p for p in glob.glob(os.path.join(args.root, "**", "*.jsonl"), recursive=True)
                   if os.path.getmtime(p) >= since)
    report = measure(files, since, args.prefix)
    tpc = report["tokens_per_char"]
    total = sum(t["chars"] for t in report["tools"].values()) or 1
    print(f"файлов {len(files)}, окно {args.days:g} дн.; расход сессий ≈ {report['token_eq'] / 1e6:.1f} млн экв. токенов"
          f"; токенов на символ {tpc:.3f}" if tpc else f"файлов {len(files)}, окно {args.days:g} дн.")
    for tool, t in sorted(report["tools"].items(), key=lambda kv: -kv[1]["chars"])[:12]:
        tok = f" ≈{int(t['chars'] * tpc)} ток." if tpc else ""
        print(f"  {tool:20s} n={t['n']:5d} символов={t['chars']:9d} ({100 * t['chars'] / total:4.1f}%)"
              f" медиана={t['median']}{tok}")
    s = report["search"]
    n = s["n"] or 1
    print(f"search: n={s['n']}; на вызов: ссылки={s['link_chars'] // n} json={s['json_chars'] // n} прочее={s['other_chars'] // n}")
    print(f"  поиск -> чтение своего результата: {100 * s['followed_by_read'] / n:.1f}%")
    print(f"  поиск -> сразу поиск: {100 * s['followed_by_search'] / n:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
