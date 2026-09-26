"""notice о выключенном поиске по смыслу в выдаче search (v1.95.0).

Ассистент видит structuredContent search, а не лог сервера и не health. Пока модель
недоступна, выдача идёт только по словам — и модель должна это знать, чтобы сказать
пользователю. В нормальном режиме выдача обязана остаться байт-в-байт прежней.
"""
import asyncio
import json
import time

import pytest

import memory_compiler.search as S
from memory_compiler import hf_offline
from memory_compiler.hf_offline import Decision
from memory_compiler.tools import call_tool


def _search(query):
    content, structured = asyncio.run(call_tool("search", {"query": query, "project": "testproj"}))
    assert len(content) == 1 and json.loads(content[0].text) == structured
    return structured


@pytest.fixture
def model_off(monkeypatch):
    monkeypatch.setattr(S, "_embed_model", None)
    monkeypatch.setattr(S, "_embed_load_error",
                        {"reason": "offline_no_cache", "detail": "x", "at": time.monotonic()})


def test_notice_when_semantic_is_off(knowledge_dir, model_off):
    structured = _search("docker")
    assert structured["count"] >= 1
    assert structured["notice"].startswith("⚠️ Поиск по смыслу выключен (offline_no_cache)")


def test_notice_on_empty_result_too(knowledge_dir, model_off):
    """Пустая выдача при выключенной семантике — самый частый обман: «знаний нет»."""
    structured = _search("квантовая телепортация пингвинов")
    assert structured["count"] == 0
    assert "Поиск по смыслу выключен" in structured["notice"]


def test_notice_during_first_download(knowledge_dir, monkeypatch):
    monkeypatch.setattr(S, "_embed_model", None)
    rid = hf_offline.repo_id(S.EMBED_MODEL_NAME, "embed")
    monkeypatch.setattr(hf_offline, "_decision", Decision("auto_download", False, (rid,), "c"))
    assert _search("docker")["notice"].startswith("⏳ Поиск по смыслу ещё не готов")


@pytest.mark.parametrize("loaded", [False, True])
def test_no_notice_when_on_or_warming_up(knowledge_dir, monkeypatch, loaded):
    monkeypatch.setattr(S, "_embed_model", object() if loaded else None)
    structured = _search("docker")
    assert "notice" not in structured, structured
