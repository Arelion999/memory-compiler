"""Observability: структурное логирование (JSON-lines), request-id, счётчики ошибок.

Лёгкий слой под Docker-на-NAS без внешних сервисов (ELK/Loki слишком тяжелы для J4125).
Пишет JSON-lines в knowledge/logs/app.jsonl с ротацией (RotatingFileHandler) + в stdout.
Даёт корреляцию tool-call ↔ логи через request_id (contextvars), счётчики ошибок по
коду/инструменту и флаг деградации semantic→BM25 — всё это отдаётся в /api/health и /api/logs.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import threading
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

import memory_compiler.config as _config

_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3


def _log_dir() -> Path:
    # Читаем KNOWLEDGE_DIR динамически с модуля config: тесты его monkeypatch'ат,
    # а в проде он стабилен (env KNOWLEDGE_DIR резолвится на импорте config).
    return Path(_config.KNOWLEDGE_DIR) / "logs"


def _app_log() -> Path:
    return _log_dir() / "app.jsonl"

# request_id для корреляции всех логов одного tool-call. По умолчанию "-".
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# ─── Счётчики (in-memory, живут до рестарта; долгую историю смотреть в app.jsonl) ───
_lock = threading.Lock()
_errors_by_code: dict[str, int] = {}
_errors_by_tool: dict[str, int] = {}
_calls_by_tool: dict[str, int] = {}
_semantic_degraded = {"v": False, "since": None}

_configured = False


class JsonLinesFormatter(logging.Formatter):
    """Одна запись = одна JSON-строка. request_id подтягивается из contextvar
    автоматически; extra-поля (tool, err_code, dur_ms) кладутся в запись как есть."""

    _STD = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        # extra-поля пользователя (logger.info(..., extra={"tool": ...}))
        for k, v in record.__dict__.items():
            if k not in self._STD and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """Идемпотентно настроить логирование: JSON-lines в файл (с ротацией) + stdout.
    Также подключает логгер mcp SDK на уровне WARNING — чтобы транспортные -32602
    ('Failed to validate request') попадали в наш app.jsonl и становились считаемыми."""
    global _configured
    if _configured:
        return
    fmt = JsonLinesFormatter()
    handlers = []
    # Файловый лог с ротацией — best-effort: если knowledge/logs не пишется (ro-mount,
    # том ещё не примонтирован), логирование НЕ должно ронять старт сервера. Тогда
    # остаётся только stdout.
    try:
        app_log = _app_log()
        app_log.parent.mkdir(parents=True, exist_ok=True)
        file_h = logging.handlers.RotatingFileHandler(
            app_log, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
        file_h.setFormatter(fmt)
        handlers.append(file_h)
    except Exception as e:
        print(f"[obs] file logging unavailable ({e}); stdout only")
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    handlers.append(stream_h)

    root = logging.getLogger("mc")
    root.setLevel(level)
    root.handlers[:] = handlers
    root.propagate = False

    # Транспортные ошибки MCP SDK (-32602/-32001) рождаются в logging.getLogger("mcp").
    mcp_log = logging.getLogger("mcp")
    mcp_log.setLevel(logging.WARNING)
    mcp_log.handlers[:] = handlers
    mcp_log.propagate = False

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Логгер под неймспейсом 'mc.*'. До setup_logging пишет в дефолтный root — безопасно."""
    return logging.getLogger(f"mc.{name}")


# ─── request_id ──────────────────────────────────────────────────────────────
def new_request_id() -> str:
    rid = uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    return rid


# ─── Счётчики ────────────────────────────────────────────────────────────────
def record_call(tool: str) -> None:
    with _lock:
        _calls_by_tool[tool] = _calls_by_tool.get(tool, 0) + 1


def record_error(tool: str, code: str | int) -> None:
    code = str(code)
    with _lock:
        _errors_by_code[code] = _errors_by_code.get(code, 0) + 1
        _errors_by_tool[tool] = _errors_by_tool.get(tool, 0) + 1


def set_semantic_degraded(flag: bool) -> None:
    with _lock:
        if flag and not _semantic_degraded["v"]:
            _semantic_degraded["since"] = datetime.now().isoformat(timespec="seconds")
        if not flag:
            _semantic_degraded["since"] = None
        _semantic_degraded["v"] = flag


def reset() -> None:
    """Сбросить счётчики и флаги (для тестов)."""
    with _lock:
        _errors_by_code.clear()
        _errors_by_tool.clear()
        _calls_by_tool.clear()
        _semantic_degraded["v"] = False
        _semantic_degraded["since"] = None


def stats() -> dict:
    """Снимок счётчиков для /api/health и /api/logs."""
    with _lock:
        return {
            "errors_by_code": dict(_errors_by_code),
            "errors_by_tool": dict(_errors_by_tool),
            "calls_by_tool": dict(_calls_by_tool),
            "total_errors": sum(_errors_by_code.values()),
            "total_calls": sum(_calls_by_tool.values()),
            "semantic_degraded": _semantic_degraded["v"],
            "semantic_degraded_since": _semantic_degraded["since"],
        }


def anomaly_alerts(prev_total_errors: int, spike_threshold: int) -> tuple[list[str], int]:
    """P2 observability: сформировать ALERT-строки по текущему состоянию + вернуть
    новый prev_total_errors. Детектит: (1) всплеск ошибок (прирост >= spike_threshold
    за интервал), (2) стойкую деградацию semantic→BM25. Чистая функция — тестируемо;
    доставку (лог/файл) делает вызывающий anomaly_loop."""
    s = stats()
    total = s["total_errors"]
    alerts: list[str] = []
    delta = total - prev_total_errors
    if delta >= spike_threshold:
        alerts.append(f"всплеск ошибок +{delta} за интервал (коды: {s['errors_by_code']})")
    if s["semantic_degraded"]:
        alerts.append(f"semantic деградировал к BM25 с {s['semantic_degraded_since']}")
    return alerts, total


def read_log_tail(limit: int = 200, level: str | None = None) -> list[dict]:
    """Прочитать хвост app.jsonl (O(limit) памяти через deque, не read_text целиком).
    Опциональный фильтр по уровню (ERROR/WARNING/...)."""
    app_log = _app_log()
    if not app_log.exists():
        return []
    want = level.upper() if level else None
    out: deque[dict] = deque(maxlen=limit)
    with app_log.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if want and rec.get("level") != want:
                continue
            out.append(rec)
    return list(out)
