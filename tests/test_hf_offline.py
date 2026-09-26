"""Офлайн-режим HF по состоянию кеша моделей (v1.95.0): решение ДО импорта ML-библиотек.

Свежая установка с HF_HUB_OFFLINE=1 по умолчанию молча оставалась без поиска по смыслу:
кеш пуст, скачать нельзя. Теперь режим выбирает сервер по кешу. Эти тесты держат схему
кеша (сверка с huggingface_hub), таблицу режимов и то, что решение доходит до библиотеки.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memory_compiler import config, hf_offline
from memory_compiler.hf_offline import Decision

REPO = Path(__file__).resolve().parent.parent
SHORT = "paraphrase-multilingual-MiniLM-L12-v2"
MINILM = "sentence-transformers/" + SHORT


def _fake_model(cache: Path, rid: str, *, ref=True, config_json=True, weights=True,
                incomplete=False, sha="0123abcd") -> Path:
    """Модель в кеше по схеме huggingface_hub: models--org--name/{refs,snapshots,blobs}."""
    repo = cache / ("models--" + rid.replace("/", "--"))
    snap = repo / "snapshots" / sha
    snap.mkdir(parents=True)
    (repo / "blobs").mkdir()
    if ref:
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text(sha, encoding="utf-8")
    if config_json:
        (snap / "config.json").write_text("{}", encoding="utf-8")
    if weights:
        (snap / "model.safetensors").write_bytes(b"\0")
    if incomplete:
        (repo / "blobs" / "0f0f0f.incomplete").write_bytes(b"\0")
    return repo


@pytest.fixture
def env(tmp_path):
    """Окружение процесса в миниатюре: пустой кеш, переменные HF не заданы."""
    return {"HF_HOME": str(tmp_path / "hf")}


def _cache(env) -> Path:
    return Path(env["HF_HOME"]) / "hub"


# ─── Где кеш и как зовётся модель ───────────────────────────────────────────

def test_hub_cache_dir_follows_library_order(tmp_path):
    base = {"XDG_CACHE_HOME": str(tmp_path / "xdg")}
    assert hf_offline.hub_cache_dir(base) == tmp_path / "xdg" / "huggingface" / "hub"
    base["HF_HOME"] = str(tmp_path / "home")
    assert hf_offline.hub_cache_dir(base) == tmp_path / "home" / "hub"
    base["HUGGINGFACE_HUB_CACHE"] = str(tmp_path / "legacy")
    assert hf_offline.hub_cache_dir(base) == tmp_path / "legacy"
    base["HF_HUB_CACHE"] = str(tmp_path / "hub")
    assert hf_offline.hub_cache_dir(base) == tmp_path / "hub"
    base["SENTENCE_TRANSFORMERS_HOME"] = str(tmp_path / "st")
    assert hf_offline.hub_cache_dir(base) == tmp_path / "st", "cache_folder ST главнее"


def test_repo_id_gives_short_names_the_st_organization():
    assert hf_offline.repo_id(SHORT) == MINILM
    assert hf_offline.repo_id("ms-marco-MiniLM-L-6-v2", "reranker") == \
        "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert hf_offline.repo_id("BAAI/bge-m3") == "BAAI/bge-m3"


@pytest.mark.parametrize("name,local", [
    ("/models/e5", True), ("./models/e5", True), ("C:\\models\\e5", True),
    ("a/b/c", True), ("BAAI/bge-m3", False), (SHORT, False),
])
def test_is_local_path(name, local):
    assert hf_offline.is_local_path(name) is local


# ─── Есть ли модель в кеше ──────────────────────────────────────────────────

def test_full_snapshot_is_cached(env):
    _fake_model(_cache(env), MINILM)
    assert hf_offline.is_cached(SHORT, "embed", env)


@pytest.mark.parametrize("broken", [
    {"ref": False}, {"config_json": False}, {"weights": False}, {"incomplete": True},
])
def test_incomplete_snapshot_is_not_cached(env, broken):
    """Каждое условие по отдельности делает снимок негодным для офлайн-загрузки."""
    _fake_model(_cache(env), MINILM, **broken)
    assert not hf_offline.is_cached(SHORT, "embed", env)


def test_empty_cache_is_not_cached(env):
    assert not hf_offline.is_cached(SHORT, "embed", env)


def test_local_path_model(tmp_path, env):
    model_dir = tmp_path / "models" / "e5"
    model_dir.mkdir(parents=True)
    assert hf_offline.is_cached(str(model_dir), "embed", env)
    assert not hf_offline.is_cached(str(tmp_path / "nope" / "e5"), "embed", env)


def test_layout_matches_huggingface_hub(env):
    """Контракт: наша схема кеша — это схема библиотеки.

    try_to_load_from_cache — чисто файловая функция hub: она находит config.json там же,
    где его ищет is_cached, и не находит без refs/main. Поменяют схему — упадёт здесь,
    а не молча на свежей установке."""
    from huggingface_hub import try_to_load_from_cache

    cache = _cache(env)
    _fake_model(cache, MINILM, sha="aaaa1111")
    found = try_to_load_from_cache(MINILM, "config.json", cache_dir=str(cache))
    assert isinstance(found, str)
    assert Path(found) == (cache / ("models--" + MINILM.replace("/", "--"))
                           / "snapshots" / "aaaa1111" / "config.json")
    assert hf_offline.is_cached(MINILM, "embed", env)

    other = "BAAI/bge-m3"
    _fake_model(cache, other, ref=False)
    assert try_to_load_from_cache(other, "config.json", cache_dir=str(cache)) is None
    assert not hf_offline.is_cached(other, "embed", env)


# ─── Решение ────────────────────────────────────────────────────────────────

def test_decide_auto_offline_when_everything_cached(env):
    _fake_model(_cache(env), MINILM)
    d = hf_offline.decide(env)
    assert (d.mode, d.offline, d.missing) == ("auto_offline", True, ())


def test_decide_auto_download_on_empty_cache(env):
    d = hf_offline.decide(env)
    assert (d.mode, d.offline, d.missing) == ("auto_download", False, (MINILM,))


@pytest.mark.parametrize("values,mode", [
    ({"HF_HUB_OFFLINE": "1"}, "explicit_offline"),
    ({"TRANSFORMERS_OFFLINE": "1"}, "explicit_offline"),
    ({"HF_HUB_OFFLINE": "0"}, "explicit_online"),
    # формула hub: HF_HUB_OFFLINE or TRANSFORMERS_OFFLINE — непустое «0» побеждает
    ({"HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "1"}, "explicit_online"),
])
def test_decide_respects_explicit_values(env, values, mode):
    env.update(values)
    d = hf_offline.decide(env)
    assert d.mode == mode
    assert d.missing == (MINILM,), "missing нужен и в явных режимах — для лога старта"


def test_empty_strings_are_not_explicit(env):
    """compose передаёт ${HF_HUB_OFFLINE:-} — пустую строку; это «решает сервер»."""
    env.update({"HF_HUB_OFFLINE": "", "TRANSFORMERS_OFFLINE": ""})
    assert hf_offline.decide(env).mode == "auto_download"


def test_reranker_checked_only_when_enabled(env):
    _fake_model(_cache(env), MINILM)
    assert hf_offline.decide(env).missing == ()
    env["RERANK_ENABLED"] = "1"
    assert hf_offline.decide(env).missing == ("BAAI/bge-reranker-v2-m3",)


def test_apply_sets_env_only_in_auto_offline(env):
    _fake_model(_cache(env), MINILM)
    d = hf_offline.apply(env)
    assert d.mode == "auto_offline" and hf_offline.current() is d
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"


@pytest.mark.parametrize("values", [{}, {"HF_HUB_OFFLINE": "1"}, {"HF_HUB_OFFLINE": "0"}])
def test_apply_leaves_env_alone_otherwise(env, values):
    env.update(values)
    before = dict(env)
    hf_offline.apply(env)
    assert env == before


def test_apply_survives_a_broken_check(env, monkeypatch, capsys):
    def boom(environ=None):
        raise PermissionError("нет доступа к кешу")
    monkeypatch.setattr(hf_offline, "decide", boom)
    before = dict(env)
    assert hf_offline.apply(env) is None
    assert env == before and hf_offline.current() is None
    assert "нет доступа к кешу" in capsys.readouterr().out


def test_apply_warns_when_ml_libraries_already_imported(env, capsys):
    """В процессе тестов huggingface_hub уже импортирован: решение auto_offline на него не
    подействует, и apply обязан это сказать вслух."""
    import huggingface_hub  # noqa: F401 — модуль гарантированно в sys.modules
    _fake_model(_cache(env), MINILM)
    hf_offline.apply(env)
    assert "после импорта huggingface_hub" in capsys.readouterr().out


def test_first_download_pending(monkeypatch):
    monkeypatch.setattr(hf_offline, "_decision", Decision("auto_download", False, (MINILM,), "c"))
    assert hf_offline.first_download_pending(SHORT)
    assert not hf_offline.first_download_pending("BAAI/bge-m3")
    monkeypatch.setattr(hf_offline, "_decision", Decision("explicit_offline", True, (MINILM,), "c"))
    assert not hf_offline.first_download_pending(SHORT)
    monkeypatch.setattr(hf_offline, "_decision", None)
    assert not hf_offline.first_download_pending(SHORT)


# ─── Причина отказа и тексты ────────────────────────────────────────────────

def test_failure_reason(env, tmp_path):
    assert hf_offline.failure_reason(SHORT, "embed", {**env, "HF_HUB_OFFLINE": "1"}) == \
        "offline_no_cache"
    assert hf_offline.failure_reason(SHORT, "embed", env) == "download_failed"
    _fake_model(_cache(env), MINILM)
    assert hf_offline.failure_reason(SHORT, "embed", env) == "load_failed"
    assert hf_offline.failure_reason(str(tmp_path / "local" / "e5"), "embed", env) == \
        "load_failed"


@pytest.mark.parametrize("reason,action", [
    ("offline_no_cache", "уберите обе переменные"),
    ("download_failed", "Проверьте доступ к huggingface.co"),
    ("load_failed", "в Docker — том hf_cache"),
    ("first_download", "первая загрузка"),
])
def test_hint_names_model_and_action(reason, action):
    text = hf_offline.hint(reason, SHORT)
    assert MINILM in text and action in text


def test_describe_every_mode():
    def text(mode, missing=(MINILM,)):
        return hf_offline.describe(Decision(mode, mode.endswith("offline"), missing, "c"))
    assert "первый запуск" in text("auto_download") and MINILM in text("auto_download")
    assert "поиск по смыслу будет выключен" in text("explicit_offline")
    assert "модели в кеше" in text("explicit_offline", ())
    assert "онлайн задан явно" in text("explicit_online")
    assert "работаю офлайн" in text("auto_offline", ())


# ─── Решение доходит до библиотеки ──────────────────────────────────────────

@pytest.mark.parametrize("cached,expected", [(True, "True"), (False, "False")])
def test_decision_reaches_huggingface_hub(tmp_path, cached, expected):
    """Сквозная проверка на НАСТОЯЩЕМ huggingface_hub в отдельном процессе: apply() до
    импорта библиотеки — и её constants.HF_HUB_OFFLINE видит решение. Ради этого решение
    принимается в server.py до импорта tools/api."""
    hf_home = tmp_path / "hf"
    if cached:
        _fake_model(hf_home / "hub", MINILM)
    drop = {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
            "SENTENCE_TRANSFORMERS_HOME", "EMBED_MODEL", "RERANK_ENABLED"}
    child_env = {k: v for k, v in os.environ.items() if k not in drop}
    child_env.update({"HF_HOME": str(hf_home), "HF_HUB_OFFLINE": "", "TRANSFORMERS_OFFLINE": ""})
    code = ("from memory_compiler import hf_offline; hf_offline.apply(); "
            "import huggingface_hub.constants as c; print(c.HF_HUB_OFFLINE)")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=child_env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == expected


# ─── Значения по умолчанию — в одном месте ──────────────────────────────────

def _string_constants(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


@pytest.mark.parametrize("module", ["search.py", "handlers_search.py", "hf_offline.py"])
def test_default_model_names_live_only_in_config(module):
    """Литералы моделей по умолчанию — только в config.py: иначе проверка кеша и загрузка
    разъедутся, и сервер будет проверять одну модель, а грузить другую."""
    consts = _string_constants(REPO / "memory_compiler" / module)
    assert config.DEFAULT_EMBED_MODEL not in consts
    assert config.DEFAULT_RERANKER_MODEL not in consts


def test_env_flag():
    assert config.env_flag("X", {"X": "TRUE"}) and config.env_flag("X", {"X": "1"})
    assert config.env_flag("X", {"X": "yes"})
    assert not config.env_flag("X", {"X": "0"}) and not config.env_flag("X", {})
