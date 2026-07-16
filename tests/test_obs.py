"""Тесты observability-слоя (obs.py) + audit error-поле + startup_prepare_index."""
import json
import logging

import pytest

from memory_compiler import obs


@pytest.fixture(autouse=True)
def _reset_obs():
    obs.reset()
    yield
    obs.reset()


# ─── Счётчики ────────────────────────────────────────────────────────────────
def test_counters_calls_and_errors():
    obs.record_call("search")
    obs.record_call("search")
    obs.record_call("save_lesson")
    obs.record_error("search", "ValueError")
    s = obs.stats()
    assert s["calls_by_tool"]["search"] == 2
    assert s["calls_by_tool"]["save_lesson"] == 1
    assert s["total_calls"] == 3
    assert s["errors_by_code"]["ValueError"] == 1
    assert s["errors_by_tool"]["search"] == 1
    assert s["total_errors"] == 1


def test_error_code_coerced_to_str():
    obs.record_error("t", -32602)
    assert obs.stats()["errors_by_code"]["-32602"] == 1


def test_semantic_degraded_toggle():
    assert obs.stats()["semantic_degraded"] is False
    obs.set_semantic_degraded(True)
    s = obs.stats()
    assert s["semantic_degraded"] is True
    assert s["semantic_degraded_since"] is not None
    obs.set_semantic_degraded(False)
    s = obs.stats()
    assert s["semantic_degraded"] is False
    assert s["semantic_degraded_since"] is None


# ─── request_id + форматтер ──────────────────────────────────────────────────
def test_new_request_id_sets_contextvar():
    rid = obs.new_request_id()
    assert len(rid) == 12
    assert obs.request_id_var.get() == rid


def test_json_formatter_valid_and_has_fields():
    obs.new_request_id()
    fmt = obs.JsonLinesFormatter()
    rec = logging.LogRecord("mc.tool", logging.INFO, __file__, 1, "hello", None, None)
    rec.tool = "search"
    rec.dur_ms = 42
    out = fmt.format(rec)
    obj = json.loads(out)  # должна быть валидная JSON-строка
    assert obj["msg"] == "hello"
    assert obj["level"] == "INFO"
    assert obj["tool"] == "search"
    assert obj["dur_ms"] == 42
    assert obj["request_id"] == obs.request_id_var.get()


# ─── read_log_tail ───────────────────────────────────────────────────────────
def _write_log(knowledge_dir, records):
    d = knowledge_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "app.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_read_log_tail_returns_last_n(knowledge_dir):
    _write_log(knowledge_dir, [{"level": "INFO", "msg": f"m{i}"} for i in range(50)])
    tail = obs.read_log_tail(limit=10)
    assert len(tail) == 10
    assert tail[-1]["msg"] == "m49"
    assert tail[0]["msg"] == "m40"


def test_read_log_tail_level_filter(knowledge_dir):
    _write_log(knowledge_dir, [
        {"level": "INFO", "msg": "ok"},
        {"level": "ERROR", "msg": "boom", "err_code": "ValueError"},
        {"level": "INFO", "msg": "ok2"},
    ])
    errs = obs.read_log_tail(limit=100, level="error")
    assert len(errs) == 1
    assert errs[0]["msg"] == "boom"


def test_read_log_tail_missing_file(knowledge_dir):
    assert obs.read_log_tail() == []


def test_read_log_tail_skips_corrupt_lines(knowledge_dir):
    d = knowledge_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "app.jsonl").write_text('{"level":"INFO","msg":"ok"}\nNOT JSON\n', encoding="utf-8")
    tail = obs.read_log_tail()
    assert len(tail) == 1
    assert tail[0]["msg"] == "ok"


# ─── audit_log с error + read_audit_log deque ────────────────────────────────
def test_audit_log_records_error_field(knowledge_dir):
    from memory_compiler.storage import audit_log, read_audit_log
    audit_log("delete_article", {"project": "p"}, 0, error="RuntimeError")
    entries = read_audit_log(10)
    assert entries[-1]["tool"] == "delete_article"
    assert entries[-1]["error"] == "RuntimeError"


def test_audit_log_no_error_field_on_success(knowledge_dir):
    from memory_compiler.storage import audit_log, read_audit_log
    audit_log("search", {"project": "p"}, 123)
    entries = read_audit_log(10)
    assert "error" not in entries[-1]
    assert entries[-1]["size"] == 123


def test_read_audit_log_tail_only(knowledge_dir):
    from memory_compiler.storage import audit_log, read_audit_log
    for i in range(30):
        audit_log("search", {"n": i}, i)
    entries = read_audit_log(5)
    assert len(entries) == 5
    assert entries[-1]["args"]["n"] == 29
    assert entries[0]["args"]["n"] == 25


# ─── startup_prepare_index ───────────────────────────────────────────────────
def test_startup_prepare_index_cold_build(knowledge_dir):
    """Нет индекса на диске → синхронно собирает, возвращает число документов."""
    import memory_compiler.search as sm
    count = sm.startup_prepare_index()
    assert count >= 1  # в knowledge_dir есть test_article.md


def test_startup_prepare_index_warm_open(knowledge_dir, monkeypatch):
    """Индекс на диске с совместимой схемой → открывает, отдаёт doc_count БЕЗ пересборки
    (синхронной нет; авто-фоновое обновление убрано в v1.9.6)."""
    import memory_compiler.search as sm
    n1 = sm.rebuild_index()
    monkeypatch.setattr(sm, "_ix", None)  # заставить открыть с диска, а не из памяти
    called = {"rebuild": False}
    monkeypatch.setattr(sm, "rebuild_index",
                        lambda: called.__setitem__("rebuild", True) or 999)
    n2 = sm.startup_prepare_index()
    assert n2 == n1                    # реальный doc_count из открытого индекса
    assert called["rebuild"] is False  # схема совпала → НЕ пересобирал


def test_startup_prepare_index_schema_change_rebuilds(knowledge_dir, monkeypatch):
    """Расхождение схемы индекса на диске и текущей SCHEMA → деструктивный пересбор
    под новую схему (иначе update_document кидал бы UnknownFieldError, а фоновый reindex
    умирал бы молча → тихая устареваемость индекса)."""
    from whoosh.fields import Schema, ID, TEXT, STORED
    import memory_compiler.search as sm
    sm.rebuild_index()  # индекс с реальной схемой на диске
    monkeypatch.setattr(sm, "_ix", None)
    # "новая" схема с дополнительным полем — не совпадёт с индексом на диске
    new_schema = Schema(
        path=ID(stored=True, unique=True), project=ID(stored=True),
        title=TEXT(stored=True), tags=TEXT(stored=True), body=TEXT(),
        preview=STORED, extra_new_field=TEXT(),
    )
    monkeypatch.setattr(sm, "SCHEMA", new_schema)
    n = sm.startup_prepare_index()
    assert n >= 1  # пересобрал под новую схему, не упал
    assert "extra_new_field" in sm.get_index().schema.names()


def test_search_candidate_pool_default():
    import memory_compiler.handlers as h
    assert isinstance(h.SEARCH_CANDIDATE_POOL, int)
    assert h.SEARCH_CANDIDATE_POOL == 10


# ─── anomaly_alerts (P2 observability) ────────────────────────────────────────
def test_anomaly_alerts_error_spike():
    for _ in range(12):
        obs.record_error("t", "X")
    alerts, new_prev = obs.anomaly_alerts(prev_total_errors=0, spike_threshold=10)
    assert new_prev == 12
    assert any("всплеск" in a for a in alerts)


def test_anomaly_alerts_below_threshold_no_alert():
    for _ in range(3):
        obs.record_error("t", "X")
    alerts, new_prev = obs.anomaly_alerts(0, 10)
    assert new_prev == 3
    assert alerts == []


def test_anomaly_alerts_semantic_degraded():
    obs.set_semantic_degraded(True)
    alerts, _ = obs.anomaly_alerts(0, 10)
    assert any("semantic" in a for a in alerts)
