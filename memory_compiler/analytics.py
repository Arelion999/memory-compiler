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
    """Сводка качества за период. Вызывать в потоке, а не в event loop.

    ⚠️ МЕТРИКА ПОЛЬЗЫ СЧИТАЕТ ДЕЙСТВИЕ, А НЕ ТОЛЬКО ЧТЕНИЕ (v1.66.0). Прежний
    `follow_rate` засчитывал успех лишь при `read_article` — и объявлял провалом
    самый частый удачный исход: ответ нашёлся прямо в превью, и сессия пошла
    ПИСАТЬ. Выдача поиска весит 13 КБ (медиана по боевому логу), там он и
    находится. Замер 26.08.2026, 525 поисков за месяц: чтение 51%, запись 17%,
    ещё поиск 22%, ничего 9% — то есть к действию ведут 68%, а не 51%.

    ⚠️ «ПЕРЕФОРМУЛИРОВОК» БОЛЬШЕ НЕТ, и возвращать их нельзя. Метрика считала
    переформулировкой ЛЮБОЙ следующий поиск в окне: из 116 таких пар текстово
    похожи (Jaccard ≥ 0.4) лишь 6 — остальное сбор контекста по РАЗНЫМ
    подтемам, то есть нормальная работа. Завышение в 19 раз, и на нём строился
    вердикт «смотреть ранжирование», к которому данные отношения не имели.
    Порог по схожести не спасает: распределение Jaccard монотонно убывает
    (50% пар вовсе без общих слов) — естественной границы нет, а подбирать её
    по тому, как красивее выглядит метрика, значит калибровать измерение под
    ответ. Остаётся наблюдаемый факт: `chained` — за поиском сразу поиск.
    """
    rows = _read_rows(hours)
    searches = [r for r in rows if r.get("tool") in SEARCH_TOOLS]

    # ⚠️ ИСХОД ОПРЕДЕЛЯЕТ ПЕРВОЕ СОБЫТИЕ ПОСЛЕ ПОИСКА, а не наличие действия в
    # окне. Прежнее `any(...)` засчитывало одно чтение сразу нескольким поискам:
    # поиск, за которым сразу пошёл другой поиск, получал зачёт за чтение,
    # случившееся уже после второго. Сверка на боевом логе (525 поисков за
    # месяц): по окну выходило 85% полезных, по первому событию — 68%, то есть
    # 88 поисков были засчитаны за чужой счёт.
    pos = {id(r): i for i, r in enumerate(rows)}
    misses, acted, chained = [], 0, 0
    for s in searches:
        if int(s.get("size") or 0) < MISS_SIZE:
            misses.append(s)
        for nxt in rows[pos[id(s)] + 1:]:
            if nxt["_ts"] - s["_ts"] > FOLLOW_SEC:
                break
            tool = nxt.get("tool")
            if tool == "read_article" or tool in WRITE_TOOLS:
                acted += 1
                break
            if tool in SEARCH_TOOLS:
                chained += 1
                break

    # Кто съедает контекст. Без этой строки приоритеты ставились вслепую: замер
    # показал, что 64% всех отданных символов приходится на search, а вовсе не
    # на стартовый контекст, который до того и оптимизировали.
    volume: dict[str, int] = {}
    for r in rows:
        size = r.get("size")
        if isinstance(size, int) and size > 0:
            volume[r.get("tool") or "?"] = volume.get(r.get("tool") or "?", 0) + size

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
        "acted": acted,
        "act_rate": round(acted / n, 3) if n else 0.0,
        "chained": chained,
        "context_bytes": sorted(volume.items(), key=lambda kv: -kv[1])[:8],
        "context_total": sum(volume.values()),
        "writes": len(writes),
        "projects": sorted(projects.items(), key=lambda kv: -kv[1])[:8],
        "miss_queries": [
            {"ts": r.get("ts"), "tool": r.get("tool"),
             "query": str((r.get("args") or {}).get("query")
                          or (r.get("args") or {}).get("tag") or "")[:80]}
            for r in misses[-10:]
        ],
    }


# ── Суточный замер (v1.73.0) ────────────────────────────────────────────────
# Контрольный замер после релизов зависел от того, вспомнит ли о нём человек, и
# ровно поэтому не делался. Прецедент в этом же проекте: `stale_facts` — ноль
# вызовов за 4.5 месяца, `knowledge_gap` — ноль. Инструмент, о котором надо
# ВСПОМНИТЬ, механизмом не работает, поэтому считает сервер по расписанию.
#
# ⚠️ НАРЕЗКА СЕССИЙ — ЭВРИСТИКА: аудит не пишет session_id, серии режутся по
# паузе. Замер 26.08.2026 показал, что доля слепых стартов ЧУВСТВИТЕЛЬНА к
# порогу (30 мин → 46%, 60 мин → 54%, 90 мин → 66%), направление при этом одно и
# то же. Поэтому порог ЗАФИКСИРОВАН: ряды сравнимы только между собой, и менять
# константу задним числом нельзя — иначе «улучшение» окажется сменой линейки,
# как уже было с baseline поиска (три роста подряд, ни один от кода).
SESSION_GAP_SEC = 3600


def _context_tools() -> set:
    """Инструменты, сами отдающие контекст: с них начатая сессия не слепая.

    Список берётся из tools.py ОТЛОЖЕННЫМ импортом — прямой дал бы цикл
    (tools тянет handlers), а копия списка разъехалась бы молча.
    """
    try:
        from memory_compiler.tools import _CONTEXT_TOOLS
        return set(_CONTEXT_TOOLS)
    except Exception:
        return {"start_task", "load_session", "get_active_context",
                "open_questions", "get_context", "get_summary"}


def _median(values: list) -> int:
    if not values:
        return 0
    s = sorted(values)
    mid = len(s) // 2
    return int(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2)


def daily(hours: float = 24.0) -> dict:
    """Суточный срез качества памяти. Вызывать в потоке, а не в event loop.

    ⚠️ Существующие формулы НЕ дублируются — miss_rate/act_rate/chained берутся
    у quality(). Копия расчёта тут уже случившаяся болезнь: act_rate живёт в трёх
    местах сразу (этот модуль, панель качества ui.py, хук mc_guard.py), четвёртая
    разъедется молча. Цена — лог читается дважды; на суточном окне это доли
    секунды, и она осознанная.
    """
    q = quality(hours)
    rows = _read_rows(hours)

    searches = [r for r in rows if r.get("tool") == "search"]
    sizes = [int(r.get("size") or 0) for r in searches if int(r.get("size") or 0) > 0]
    total_chars = sum(int(r["size"]) for r in rows
                      if isinstance(r.get("size"), int) and r["size"] > 0)

    finishes = [r for r in rows if r.get("tool") == "finish_task"]
    no_summary = [r for r in finishes
                  if not str((r.get("args") or {}).get("session_summary") or "").strip()]

    # ⚠️ ДВЕ ЦИФРЫ, А НЕ ОДНА, и этого требует сама нарезка. Длинная непрерывная
    # работа режется паузами на несколько серий, и КАЖДОЕ продолжение по
    # определению начинается не со start_task — «вслепую 100%» получается
    # артефактом сегментации, а не фактом о работе. Первый живой прогон 28.08
    # это и показал: 4 серии из 4 «слепые», хотя контекст в тот день грузили.
    # Исходный замер 26.08 давал пару ровно поэтому: контекст где-либо 54%,
    # контекст первым вызовом 21%.
    ctx = _context_tools()
    sessions, blind, ctx_anywhere, prev_ts = 0, 0, 0, None
    seen_ctx = False
    for r in rows:
        if prev_ts is None or r["_ts"] - prev_ts > SESSION_GAP_SEC:
            if sessions and seen_ctx:
                ctx_anywhere += 1
            sessions += 1
            seen_ctx = False
            if r.get("tool") not in ctx:
                blind += 1
        if r.get("tool") in ctx:
            seen_ctx = True
        prev_ts = r["_ts"]
    if sessions and seen_ctx:
        ctx_anywhere += 1

    nf = len(finishes)
    return {
        "hours": hours,
        "calls": q["calls"],
        "searches": q["searches"],
        "miss_rate": q["miss_rate"],
        "act_rate": q["act_rate"],
        "chained": q["chained"],
        "search_median": _median(sizes),
        "search_share": round(sum(sizes) / total_chars, 3) if total_chars else 0.0,
        "notes": len([r for r in rows if r.get("tool") == "session_note"]),
        "finish_total": nf,
        "finish_no_summary": len(no_summary),
        "no_summary_rate": round(len(no_summary) / nf, 3) if nf else 0.0,
        "sessions": sessions,
        "blind": blind,
        "blind_rate": round(blind / sessions, 3) if sessions else 0.0,
        "ctx_anywhere": ctx_anywhere,
        "ctx_rate": round(ctx_anywhere / sessions, 3) if sessions else 0.0,
    }
