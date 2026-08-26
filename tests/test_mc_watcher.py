"""Поведение авто-рестарта mc-watcher (v1.57.0).

Каждый рестарт убивает MCP-сессии клиентов, и следующий вызов падает с -32001
(замер 2026-07-20: 76 рестартов за сутки). Cooldown снижает их число, но НЕ
должен задерживать релиз — смена VERSION проходит мимо ограничения.

Скрипт исполняется по-настоящему: логика живёт в bash, и проверять её пересказом
на Python значило бы тестировать пересказ.
"""

import os
import pathlib
import shutil
import subprocess
import time

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash недоступен")

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "mc-watcher.sh")


@pytest.fixture
def env(tmp_path):
    """Песочница: свой каталог исходников, свой «docker», свои файлы состояния."""
    mc = tmp_path / "memory_compiler"
    mc.mkdir()
    (mc / "tools.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.0.0", encoding="utf-8")

    calls = tmp_path / "docker-calls.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text('#!/bin/bash\necho "$@" >> "%s"\n' % calls.as_posix(),
                           encoding="utf-8", newline="\n")
    fake_docker.chmod(0o755)

    e = dict(os.environ)
    e.update({
        "MC_DIR": mc.as_posix(),
        "VERSION_FILE": (tmp_path / "VERSION").as_posix(),
        "MC_STATE": (tmp_path / "state").as_posix(),
        "MC_PENDING": (tmp_path / "pending").as_posix(),
        "MC_LOG": (tmp_path / "log").as_posix(),
        "MC_LAST_TS": (tmp_path / "last").as_posix(),
        "MC_VER_STATE": (tmp_path / "vstate").as_posix(),
        "DOCKER": fake_docker.as_posix(),
        "CONTAINER": "test-container",
    })
    return {"env": e, "tmp": tmp_path, "mc": mc, "calls": calls}


def run(env):
    subprocess.run([BASH, SCRIPT], env=env["env"], check=True,
                   capture_output=True, timeout=60)


def restarts(env):
    if not env["calls"].exists():
        return 0
    return len([l for l in env["calls"].read_text(encoding="utf-8").splitlines() if l.strip()])


def deploy(env):
    """Довести изменение до рестарта: первый прогон — debounce, второй — рестарт."""
    run(env); run(env)


def test_debounce_holds_first_run(env):
    run(env)
    assert restarts(env) == 0, "рестарт на первом же прогоне — синк мог не устояться"
    run(env)
    assert restarts(env) == 1


def test_no_restart_without_changes(env):
    deploy(env)
    run(env); run(env)
    assert restarts(env) == 1, "рестарт без изменения кода"


def test_cooldown_holds_code_change(env):
    deploy(env)
    (env["mc"] / "tools.py").write_text("x = 2\n", encoding="utf-8")
    run(env); run(env)
    assert restarts(env) == 1, "cooldown не удержал вторую правку подряд"
    log = (env["tmp"] / "log").read_text(encoding="utf-8")
    assert "change held" in log


def test_version_change_bypasses_cooldown(env):
    """Релиз обязан применяться сразу: его проверяют живым вызовом, а не ждут."""
    deploy(env)
    (env["mc"] / "tools.py").write_text("x = 3\n", encoding="utf-8")
    (env["tmp"] / "VERSION").write_text("1.0.1", encoding="utf-8")
    run(env); run(env)
    assert restarts(env) == 2, "смена VERSION не должна попадать под cooldown"


def test_change_is_not_lost_after_cooldown(env):
    """Удержанное изменение обязано доехать, а не потеряться."""
    deploy(env)
    (env["mc"] / "tools.py").write_text("x = 4\n", encoding="utf-8")
    run(env); run(env)
    assert restarts(env) == 1
    (env["tmp"] / "last").write_text(str(int(time.time()) - 10_000), encoding="utf-8")
    run(env)
    assert restarts(env) == 2, "после истечения cooldown правка не применилась"


def test_empty_source_dir_does_nothing(env):
    """Каталог недоступен во время синка — молчим, а не рестартуем на пустоте."""
    for f in env["mc"].glob("*.py"):
        f.unlink()
    (env["tmp"] / "VERSION").unlink()
    run(env); run(env)
    assert restarts(env) == 0


def test_missing_source_dir_is_loud_not_silent(env, tmp_path):
    """Отсутствие каталога исходников обязано попасть в лог.

    Живой случай 2026-08-26: рабочую копию скрипта перезаписали версией из
    репозитория с шаблонным MC_DIR. Деплой встал, а в логе не было ни строчки —
    молчание выглядело как «изменений нет».
    """
    env["env"]["MC_DIR"] = (tmp_path / "нет-такого-каталога").as_posix()
    env["env"]["MC_MISSING_STAMP"] = (env["tmp"] / "missing").as_posix()
    proc = subprocess.run([BASH, SCRIPT], env=env["env"], capture_output=True, timeout=60)
    assert proc.returncode == 1, "молчаливый успех при отсутствующем MC_DIR"
    log = (env["tmp"] / "log").read_text(encoding="utf-8")
    assert "MC_DIR не существует" in log
    assert restarts(env) == 0


# ── скрипт бэкапа: шаблоны исключений ───────────────────────────────────────

def _bash_path(p) -> str:
    """Windows-путь в вид, понятный MSYS-инструментам внутри Git Bash.

    `C:/Users/...` их ломает: tar пытается разрешить «C:» как хост и падает
    (тот же класс, что описан в статье про verify off-site бэкапа). Нужен /c/...
    """
    s = pathlib.Path(p).as_posix()
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


BACKUP_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "scripts", "mc-backup.sh")


def test_backup_excludes_cover_neighbouring_names(tmp_path):
    """Исключения обязаны ловить имена РЯДОМ, а не только точные.

    Живой случай 2026-08-26: `--exclude=".whoosh_index"` не покрывал
    `.whoosh_index.corrupt-20260819/`, и 44 МБ битого индекса уезжали в каждый
    ежедневный архив. Та же дыра в .gitignore базы раздула историю до 590 МБ.
    """
    kb = tmp_path / "knowledge"
    (kb / "proj").mkdir(parents=True)
    (kb / "proj" / "a.md").write_text("статья", encoding="utf-8")
    for junk in (".whoosh_index", ".whoosh_index.corrupt-20260819", ".git.old"):
        (kb / junk).mkdir()
        (kb / junk / "big.seg").write_text("x" * 5000, encoding="utf-8")
    (kb / ".embeddings.pkl").write_text("y" * 5000, encoding="utf-8")
    (kb / ".embeddings.pkl.bak-v1180").write_text("y" * 5000, encoding="utf-8")
    (kb / ".embeddings_HOST_Conflict.pkl").write_text("y" * 5000, encoding="utf-8")

    env = dict(os.environ)
    env.update({"KB_DIR": _bash_path(kb),
                "BACKUP_DIR": _bash_path(tmp_path / "backups"),
                "MC_BACKUP_LOG": _bash_path(tmp_path / "backup.log")})
    subprocess.run([BASH, BACKUP_SCRIPT], env=env, check=True, capture_output=True, timeout=120)

    archives = list((tmp_path / "backups").glob("knowledge-*.tar.gz"))
    assert archives, "архив не создан"
    listing = subprocess.run(["tar", "-tzf", archives[0].as_posix()],
                             capture_output=True, text=True, timeout=60).stdout
    assert "proj/a.md" in listing, "статья не попала в архив — исключения слишком широкие"
    for junk in (".whoosh_index", ".embeddings.pkl", ".git.old", "Conflict"):
        assert junk not in listing, "в архив попало восстановимое или мусор: %s" % junk
