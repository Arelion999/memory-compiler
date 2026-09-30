"""Фоновый git-коммит (2026-09-30): запись в базу не ждёт git add -A.

git add -A по всей базе — 5,5–8,6 с (замер 2026-07-20 на 1815 статьях, на NAS
дольше), и каждый save_lesson ждал его в to_thread. Хендлеры переведены на
storage.git_commit_background: коммит уходит в daemon-поток, вызов возвращается
сразу. Синхронный git_commit остаётся для maintenance-проходов и тестов.

Тесты гоняют НАСТОЯЩИЙ git (как test_git_service_files.py): суть в том, что
коммит реально появляется в истории, а подменённый subprocess это не проверит.
"""
import shutil
import subprocess
import threading
import time

import pytest

from memory_compiler import storage

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git не установлен")


def _git(kd, *args):
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=str(kd),
                          capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _init_repo(kd):
    """Репо с первым коммитом: storage-функции ходят в patched KNOWLEDGE_DIR."""
    _git(kd, "init", "-q")
    _git(kd, "config", "user.email", "test@example.com")
    _git(kd, "config", "user.name", "test")
    storage.git_commit("init")


def _wait_commit(kd, message, timeout=10.0):
    """Полл git log, пока сообщение не появится в истории. True — успел."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        subjects = _git(kd, "log", "--format=%s", "-n", "10").stdout.splitlines()
        if message in subjects:
            return True
        time.sleep(0.05)
    return False


def _wait_worker_idle(timeout=10.0):
    """Дождаться, что фоновый воркер завершился (дрейн между тестами).

    Живой воркер от прошлого теста заставил бы git_commit_background МОЛЧА
    пропустить вызов — тесты бы гонялись. Фикстура локальная: глобальное
    поведение других тестов не меняет.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = storage._commit_worker
        if worker is None or not worker.is_alive():
            return
        time.sleep(0.05)
    raise AssertionError("фоновый git-коммит не завершился за %s с" % timeout)


@pytest.fixture
def drained():
    _wait_worker_idle()
    yield
    _wait_worker_idle()


def test_background_commit_returns_fast_and_lands_in_history(knowledge_dir, drained):
    """Возврат <0,5 с после записи, а коммит с сообщением — в git log (полл 10 с)."""
    kd = knowledge_dir
    _init_repo(kd)
    (kd / "testproj" / "новая.md").write_text("# Новая\n", encoding="utf-8")

    start = time.monotonic()
    storage.git_commit_background("save: фоновая")
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, "вызов ждёт коммит: %.2f с" % elapsed

    assert _wait_commit(kd, "save: фоновая"), (
        "коммит не появился в истории: %s" % _git(kd, "log", "--format=%s").stdout)
    assert "testproj/новая.md" in _git(kd, "ls-files").stdout


def test_two_saves_back_to_back_both_land(knowledge_dir, drained):
    """Два вызова подряд с двумя файлами: оба в истории, без исключений."""
    kd = knowledge_dir
    _init_repo(kd)
    (kd / "testproj" / "a.md").write_text("# A\n", encoding="utf-8")
    storage.git_commit_background("save: первая")
    assert _wait_commit(kd, "save: первая")
    # ⚠️ Между «коммит виден в log» и «воркер завершился» есть окно: второй
    # вызов в нём был бы МОЛЧА пропущен (дизайн), и «save: вторая» не появилась
    # бы вовсе — флакоть под нагрузкой. Дожидаемся именно завершения воркера.
    _wait_worker_idle()
    (kd / "testproj" / "b.md").write_text("# B\n", encoding="utf-8")
    storage.git_commit_background("save: вторая")
    assert _wait_commit(kd, "save: вторая")
    tracked = _git(kd, "ls-files").stdout
    assert "testproj/a.md" in tracked and "testproj/b.md" in tracked


def test_call_while_worker_alive_is_skipped_silently(knowledge_dir, drained,
                                                     monkeypatch):
    """Второй вызов при живом воркере: мгновенный возврат, без ошибок, пропуск.

    Замедленный _commit_locked имитирует долгий add -A: пока воркер держит
    замок, git_commit_background обязан не ждать и не плодить второй поток.
    """
    kd = knowledge_dir
    _init_repo(kd)
    started = threading.Event()
    release = threading.Event()

    def slow_commit(message):
        started.set()
        assert release.wait(10)

    monkeypatch.setattr(storage, "_commit_locked", slow_commit)
    storage.git_commit_background("первый")
    assert started.wait(5), "воркер не стартовал"

    start = time.monotonic()
    storage.git_commit_background("второй")  # пропущен: воркер жив
    assert time.monotonic() - start < 0.5, "второй вызов ждал живой воркер"
    assert storage._commit_worker.is_alive(), "второй вызов не должен плодить поток"

    release.set()
    storage._commit_worker.join(10)
    assert not storage._commit_worker.is_alive()
