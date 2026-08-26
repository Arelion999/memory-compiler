"""Упаковка git-репозитория базы по ВЕСУ, а не только по числу объектов (v1.59.1).

Замер 2026-08-26: 4294 рыхлых объекта весом 514 МБ. Порог `git gc --auto`
считает штуки (6700), поэтому еженедельный проход был no-op, а `.git` разросся
до 590 МБ при 23 МБ статей — крупные блобы (`_audit.log` по версии на каждое
сохранение) веса порогу не добавляли.
"""

import subprocess

import pytest

from memory_compiler import storage


@pytest.fixture
def fake_git(monkeypatch, tmp_path):
    """Перехватываем вызовы git: возвращаем заданный вес и пишем аргументы."""
    calls = []

    def make(size_kb):
        def _run(cmd, **kw):
            calls.append(cmd)
            if "count-objects" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="count: 4294\nsize: %d\n" % size_kb, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        monkeypatch.setattr(storage.subprocess, "run", _run)
        monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
        return calls

    return make


def test_heavy_loose_objects_force_full_pack(fake_git):
    """514 МБ рыхлых — пакуем принудительно, не спрашивая git."""
    calls = fake_git(514 * 1024)
    storage.git_gc()
    gc_cmd = [c for c in calls if "gc" in c][-1]
    assert "--auto" not in gc_cmd, "при 514 МБ рыхлых объектов --auto оставит их лежать"
    assert "gc" in gc_cmd


def test_light_repo_leaves_decision_to_git(fake_git):
    """Пока вес мал, решение о упаковке — за git: полный gc идёт минутами."""
    calls = fake_git(20 * 1024)
    storage.git_gc()
    gc_cmd = [c for c in calls if "gc" in c][-1]
    assert "--auto" in gc_cmd


def test_threshold_boundary(fake_git):
    calls = fake_git(int(storage.GC_LOOSE_MB * 1024))
    storage.git_gc()
    assert "--auto" not in [c for c in calls if "gc" in c][-1], "ровно на пороге тоже пакуем"


def test_loose_size_parsed_in_megabytes(fake_git):
    fake_git(514 * 1024)
    assert abs(storage.loose_objects_mb() - 514.0) < 0.1


def test_git_unavailable_is_survivable(monkeypatch, tmp_path):
    """Нет git — не падаем: упакуемся на следующей неделе."""
    def _boom(*a, **k):
        raise FileNotFoundError("git not found")
    monkeypatch.setattr(storage.subprocess, "run", _boom)
    monkeypatch.setattr(storage, "KNOWLEDGE_DIR", tmp_path)
    assert storage.loose_objects_mb() == 0.0
    storage.git_gc()          # не должно бросить


def test_autodetach_stays_disabled(fake_git):
    """autoDetach=false обязателен: отсоединённый gc оседал зомби на PID 1."""
    calls = fake_git(514 * 1024)
    storage.git_gc()
    gc_cmd = [c for c in calls if "gc" in c][-1]
    assert "gc.autoDetach=false" in gc_cmd
