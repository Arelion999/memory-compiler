"""Состояние поиска по смыслу (v1.95.0): отказ загрузки модели больше не немой.

Воспроизведение 25.09.2026 (пустой HF_HOME + офлайн-флаги compose): warm_models глотал
OSError, health вечно отдавал models_ready=false, а semantic_degraded не взводился —
semantic_search при пустых эмбеддингах выходит до модели. Эти тесты держат, что отказ
запоминается с причиной, повтор не долбит сеть и запрос не ждёт первую загрузку.
"""
import time

import numpy as np
import pytest

import memory_compiler.search as S
from memory_compiler import hf_offline, obs
from memory_compiler.hf_offline import Decision


class _Boom(Exception):
    pass


@pytest.fixture
def no_model(monkeypatch):
    """Модель не загружена и отказов не было — как на старте процесса."""
    monkeypatch.setattr(S, "_embed_model", None)
    monkeypatch.setattr(S, "_embed_load_error", None)


def _first_download(monkeypatch):
    rid = hf_offline.repo_id(S.EMBED_MODEL_NAME, "embed")
    monkeypatch.setattr(hf_offline, "_decision", Decision("auto_download", False, (rid,), "c"))


def _failing_ctor(calls):
    def ctor(name, *a, **kw):
        calls.append(name)
        raise _Boom("нет в кеше")
    return ctor


def test_load_failure_is_recorded_with_reason_and_reraised(no_model, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "SentenceTransformer", _failing_ctor(calls))
    monkeypatch.setattr(hf_offline, "failure_reason",
                        lambda name, kind="embed", environ=None: "offline_no_cache")
    with pytest.raises(_Boom):
        S.get_embed_model()
    assert calls == [S.EMBED_MODEL_NAME]
    assert S.semantic_state() == ("off", "offline_no_cache")
    assert "нет в кеше" in S.semantic_error_detail()


def test_failure_reason_crash_falls_back_and_still_records(no_model, monkeypatch):
    """F2 (final-fix-findings.md): hf_offline.failure_reason сам может упасть (например,
    PermissionError от rglob по нечитаемому снимку). Раньше это подменяло исходное
    исключение и _embed_load_error не записывался вовсе — без причины и без паузы,
    состояние оставалось "loading"."""
    calls = []
    monkeypatch.setattr(S, "SentenceTransformer", _failing_ctor(calls))

    def boom_reason(name, kind="embed", environ=None):
        raise PermissionError("нечитаемый снимок")
    monkeypatch.setattr(hf_offline, "failure_reason", boom_reason)
    with pytest.raises(_Boom):
        S.get_embed_model()
    assert S.semantic_state() == ("off", "load_failed")


def test_retry_waits_for_the_pause(no_model, monkeypatch):
    calls = []
    monkeypatch.setattr(S, "SentenceTransformer", _failing_ctor(calls))
    with pytest.raises(_Boom):
        S.get_embed_model()
    with pytest.raises(S.EmbedModelUnavailable) as err:
        S.get_embed_model()
    assert len(calls) == 1, "в пределах паузы конструктор не зовётся — сеть не долбим"
    assert err.value.reason == S.semantic_state()[1]
    monkeypatch.setattr(S, "EMBED_RETRY_SEC", 0)
    with pytest.raises(_Boom):
        S.get_embed_model()
    assert len(calls) == 2, "после паузы — новая попытка"


def test_success_clears_the_failure(no_model, monkeypatch):
    monkeypatch.setattr(S, "EMBED_RETRY_SEC", 0)
    fail = {"on": True}

    class Model:
        max_seq_length = 128

    def ctor(name, *a, **kw):
        if fail["on"]:
            raise _Boom("x")
        return Model()

    monkeypatch.setattr(S, "SentenceTransformer", ctor)
    with pytest.raises(_Boom):
        S.get_embed_model()
    fail["on"] = False
    assert isinstance(S.get_embed_model(), Model)
    assert S.semantic_state() == ("on", None)
    assert S.semantic_error_detail() is None


def test_state_loading_vs_first_download(no_model, monkeypatch):
    assert S.semantic_state() == ("loading", None)
    _first_download(monkeypatch)
    assert S.semantic_state() == ("loading", "first_download")
    rid = hf_offline.repo_id(S.EMBED_MODEL_NAME, "embed")
    monkeypatch.setattr(hf_offline, "_decision", Decision("explicit_offline", True, (rid,), "c"))
    assert S.semantic_state() == ("loading", None), "первая загрузка — только в auto_download"


def test_notice_texts(no_model, monkeypatch):
    assert S.semantic_notice() == ""
    monkeypatch.setattr(S, "_embed_load_error",
                        {"reason": "offline_no_cache", "detail": "d", "at": time.monotonic()})
    note = S.semantic_notice()
    assert note.startswith("⚠️ Поиск по смыслу выключен (offline_no_cache): ")
    assert "только по словам" in note
    monkeypatch.setattr(S, "_embed_load_error", None)
    _first_download(monkeypatch)
    assert S.semantic_notice().startswith("⏳ Поиск по смыслу ещё не готов: ")
    monkeypatch.setattr(S, "_embed_model", object())
    assert S.semantic_notice() == "", "модель загружена — выдача без notice"


def test_query_does_not_wait_for_first_download(no_model, monkeypatch):
    _first_download(monkeypatch)

    def ctor(name, *a, **kw):
        raise AssertionError("запрос не должен грузить модель во время первой загрузки")

    monkeypatch.setattr(S, "SentenceTransformer", ctor)
    with pytest.raises(S.EmbedModelUnavailable) as err:
        S.encode_query("вопрос")
    assert err.value.reason == "first_download"


def test_whoosh_search_falls_back_to_bm25_quietly(knowledge_dir, no_model, monkeypatch, capsys):
    S.rebuild_index()
    monkeypatch.setattr(S, "_embeddings",
                        {"testproj/test_article.md": np.ones(3, dtype=np.float32)})
    monkeypatch.setattr(S, "_embed_load_error",
                        {"reason": "download_failed", "detail": "d", "at": time.monotonic()})
    obs.reset()
    results = S.whoosh_search("docker", limit=5)
    assert any(r["file"] == "test_article.md" for r in results), "BM25 обязан отдать статью"
    assert obs.stats()["semantic_degraded"] is True
    assert "Semantic search failed" not in capsys.readouterr().out


def test_real_library_offline_missing_model(no_model, monkeypatch):
    """Настоящий sentence-transformers в офлайне на модели, которой нет ни в чьём кеше, —
    ровно свежая установка. Сети нет: conftest ставит HF_HUB_OFFLINE=1 до импорта."""
    if not hf_offline.effective_offline():
        pytest.skip("тест только офлайн: онлайн он пошёл бы в сеть")
    monkeypatch.setattr(S, "EMBED_MODEL_NAME", "mc-test-org/definitely-not-cached-model")
    with pytest.raises(Exception):
        S.get_embed_model()
    assert S.semantic_state() == ("off", "offline_no_cache")
