"""Поиск по смыслу в /api/health и логе старта (v1.95.0).

До v1.95.0 свежая установка без модели отвечала status: ok, models_ready: false —
навсегда и неотличимо от «ещё грузится». Теперь health называет состояние и причину
публично, а подробности (текст ошибки, решение об офлайн-режиме) — только под auth.
"""
import asyncio
import json
import time

import memory_compiler.api as api
import memory_compiler.search as S
from memory_compiler import hf_offline
from memory_compiler.hf_offline import Decision


class Req:
    """Стенд под starlette.Request: health читает headers и cookies (см. test_mcp_sessions)."""

    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


def _health(req=None):
    return json.loads(asyncio.run(api.web_health(req or Req())).body)


def _failed(reason, detail="d"):
    return {"reason": reason, "detail": detail, "at": time.monotonic()}


def test_public_health_names_state_and_reason(knowledge_dir, monkeypatch):
    monkeypatch.setattr(api, "MC_API_KEY", "k")  # auth настроен, запрос без ключа
    monkeypatch.setattr(S, "_embed_model", None)
    monkeypatch.setattr(S, "_embed_load_error", _failed("offline_no_cache", "путь /root/.cache/x"))
    body = _health()
    assert (body["semantic"], body["semantic_reason"]) == ("off", "offline_no_cache")
    assert body["status"] == "ok" and body["models_ready"] is False
    assert "observability" not in body, "детали — только под auth"
    assert "/root/.cache" not in json.dumps(body, ensure_ascii=False)


def test_public_health_loading_and_on(knowledge_dir, monkeypatch):
    monkeypatch.setattr(api, "MC_API_KEY", "k")
    monkeypatch.setattr(S, "_embed_model", None)
    body = _health()
    assert (body["semantic"], body["semantic_reason"]) == ("loading", None)
    rid = hf_offline.repo_id(S.EMBED_MODEL_NAME, "embed")
    monkeypatch.setattr(hf_offline, "_decision", Decision("auto_download", False, (rid,), "c"))
    body = _health()
    assert (body["semantic"], body["semantic_reason"]) == ("loading", "first_download")
    monkeypatch.setattr(S, "_embed_model", object())
    body = _health()
    assert (body["semantic"], body["semantic_reason"], body["models_ready"]) == ("on", None, True)


def test_authed_health_carries_error_and_decision(knowledge_dir, monkeypatch):
    monkeypatch.setattr(api, "MC_API_KEY", "")  # auth не настроен → полный ответ
    monkeypatch.setattr(S, "_embed_model", None)
    monkeypatch.setattr(S, "_embed_load_error", _failed("download_failed", "ConnectError"))
    d = Decision("auto_download", False, ("sentence-transformers/x",), "/c")
    monkeypatch.setattr(hf_offline, "_decision", d)
    obsv = _health()["observability"]
    assert obsv["semantic_error"] == "ConnectError"
    assert obsv["hf_offline"] == d.as_dict()


def test_warm_failure_names_reason_and_action(monkeypatch, capsys):
    monkeypatch.setattr(S, "_embed_model", None)
    monkeypatch.setattr(S, "_embed_load_error", None)

    def boom():
        S._embed_load_error = _failed("offline_no_cache")
        raise RuntimeError("нет модели в кэше")

    monkeypatch.setattr(S, "get_embed_model", boom)
    assert asyncio.run(api.warm_models()) == 0.0, "сбой прогрева по-прежнему не роняет старт"
    out = capsys.readouterr().out
    assert "Поиск по смыслу выключен (offline_no_cache)" in out
    assert "уберите обе переменные" in out


def test_warm_failure_without_recorded_reason_is_load_failed(monkeypatch, capsys):
    monkeypatch.setattr(S, "_embed_model", None)
    monkeypatch.setattr(S, "_embed_load_error", None)

    def boom():
        raise RuntimeError("что-то другое")

    monkeypatch.setattr(S, "get_embed_model", boom)
    asyncio.run(api.warm_models())
    assert "Поиск по смыслу выключен (load_failed)" in capsys.readouterr().out


def test_warm_failure_after_model_loaded_is_not_a_false_alarm(monkeypatch, capsys):
    """F3 (final-fix-findings.md): если get_embed_model успел загрузить модель и упал
    только фиктивный encode_query (прогрев), semantic_state — ("on", None). Раньше
    warm_models всё равно печатал «Поиск по смыслу выключен (load_failed)» — ложную
    тревогу, хотя модель загружена и поиск по смыслу работает."""
    monkeypatch.setattr(S, "_embed_model", None)
    monkeypatch.setattr(S, "_embed_load_error", None)

    def fake_get_embed_model():
        S._embed_model = object()
        return S._embed_model

    def fake_encode_query(text):
        raise RuntimeError("фиктивный encode не удался")

    monkeypatch.setattr(S, "get_embed_model", fake_get_embed_model)
    monkeypatch.setattr(S, "encode_query", fake_encode_query)
    assert asyncio.run(api.warm_models()) == 0.0
    out = capsys.readouterr().out
    assert "Поиск по смыслу выключен" not in out
    assert "модель загружена" in out


def test_startup_reports_hf_decision(monkeypatch, capsys):
    monkeypatch.setattr(hf_offline, "_decision",
                        Decision("explicit_offline", True, ("sentence-transformers/x",), "/c"))
    api._report_hf_decision()
    out = capsys.readouterr().out
    assert "[hf] HF: офлайн задан явно, а в кеше нет sentence-transformers/x" in out


def test_startup_report_is_silent_without_decision(monkeypatch, capsys):
    monkeypatch.setattr(hf_offline, "_decision", None)
    api._report_hf_decision()
    assert "[hf]" not in capsys.readouterr().out
