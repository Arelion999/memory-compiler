"""Метрики качества памяти как продукта — из аудит-лога.

Отвечает на вопрос «работает ли поиск», а не «сколько было вызовов». Источник —
`knowledge/_audit.log`: он пишется на каждом успешном MCP-вызове и содержит
инструмент, аргументы и размер ответа.

⚠️ РАЗМЕР ОТВЕТА — РАБОЧИЙ ПРИЗНАК ПРОМАХА, и порог взят замером, а не на глаз:
на боевом логе (1130 поисков) медиана выдачи 7953 символа, p10 = 910, а у
запросов, не нашедших ничего, — 29..56. Отсюда MISS_SIZE = 200: он ловит
«ничего не найдено» и не задевает короткие, но содержательные ответы.

⚠️ СВЯЗКА «поиск -> чтение» ПРИБЛИЗИТЕЛЬНА. Аудит не пишет session_id, поэтому
чтение сопоставляется с поиском по времени и глобально: при параллельных
сессиях возможен перехлёст. Для вопроса «часто ли выдача вообще пригождается»
этого достаточно; точные цифры ранжирования даёт retrieval_eval.py на
golden-наборе, и подменять его этим модулем нельзя.

⚠️ ЧТЕНИЕ ЛОГА — ТЯЖЁЛОЕ (файл в мегабайтах). Из async-хендлера вызывать
только через asyncio.to_thread, иначе встаёт весь сервер: ровно этот класс
дефекта уже ловили дважды (rerank в search, git_commit в 13 хендлерах).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from memory_compiler.config import KNOWLEDGE_DIR

AUDIT_TS_FMT = "%Y-%m-%d %H:%M:%S"
MISS_SIZE = 200
FOLLOW_SEC = 180
TAIL_BYTES = 20_000_000

SEARCH_TOOLS = {"search", "search_by_tag", "search_error", "search_decisions",
                "search_snippets", "ask"}
WRITE_TOOLS = {"save_lesson", "save_decision", "save_runbook", "save_secret",
               "save_tracking", "save_session", "save_from_template",
               "save_contexts", "save_compact", "finish_task", "edit_article",
               "delete_article", "consolidate", "ingest"}


def _audit_path() -> Path:
    return Path(KNOWLEDGE_DIR) / "_audit.log"


def _read_rows(hours: float) -> list[dict]:
    path = _audit_path()
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()
            data = f.read()
    except Exception:
        return []
    since = time.time() - hours * 3600
    rows = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
            ts = datetime.strptime(rec.get("ts", ""), AUDIT_TS_FMT).timestamp()
        except Exception:
            continue
        if ts >= since:
            rec["_ts"] = ts
            rows.append(rec)
    rows.sort(key=lambda r: r["_ts"])
    return rows


def quality(hours: float = 168.0) -> dict:
    """Сводка качества за период. Вызывать в потоке, а не в event loop."""
    rows = _read_rows(hours)
    searches = [r for r in rows if r.get("tool") in SEARCH_TOOLS]
    reads = [r["_ts"] for r in rows if r.get("tool") == "read_article"]

    misses, followed, reformulations = [], 0, 0
    for i, s in enumerate(searches):
        if int(s.get("size") or 0) < MISS_SIZE:
            misses.append(s)
        if any(0 <= t - s["_ts"] <= FOLLOW_SEC for t in reads):
            followed += 1
        elif i + 1 < len(searches) and 0 <= searches[i + 1]["_ts"] - s["_ts"] <= FOLLOW_SEC:
            reformulations += 1

    writes = [r for r in rows if r.get("tool") in WRITE_TOOLS]
    projects: dict[str, int] = {}
    for r in writes:
        proj = (r.get("args") or {}).get("project") or "?"
        projects[proj] = projects.get(proj, 0) + 1

    n = len(searches)
    return {
        "hours": hours,
        "calls": len(rows),
        "searches": n,
        "misses": len(misses),
        "miss_rate": round(len(misses) / n, 3) if n else 0.0,
        "followed": followed,
        "follow_rate": round(followed / n, 3) if n else 0.0,
        "reformulations": reformulations,
        "writes": len(writes),
        "projects": sorted(projects.items(), key=lambda kv: -kv[1])[:8],
        "miss_queries": [
            {"ts": r.get("ts"), "tool": r.get("tool"),
             "query": str((r.get("args") or {}).get("query")
                          or (r.get("args") or {}).get("tag") or "")[:80]}
            for r in misses[-10:]
        ],
    }
