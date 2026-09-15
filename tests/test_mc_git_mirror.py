"""Зеркало git-истории базы на ПК не пухнет от ежедневных прогонов (v1.88.1).

15.09.2026: bare-зеркало весило 422 МиБ в 17 пак-файлах при 32,8 МБ уникальных
объектов. Снимок knowledge-git.bundle ПОЛНЫЙ (mc-git-bundle.sh: git bundle create
--all), а у fetch из бандла нет согласования — каждый прогон клал весь пак заново.
Замер это подтвердил: число объектов в паках росло день ото дня, в последнем паке
было ровно столько, сколько уникальных объектов во всём зеркале. Лечится
переупаковкой после fetch.

Скрипт исполняется по-настоящему (pwsh + git): логика живёт в PowerShell, и
пересказ на Python проверял бы пересказ.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "mc-git-mirror.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None or shutil.which("git") is None,
                                reason="нужны pwsh и git")


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _source(tmp_path):
    """Источник снимка: 60 статей — скрипт требует в HEAD не меньше 50 файлов."""
    src = tmp_path / "base"
    src.mkdir()
    _git("init", "-q", cwd=src)
    _git("config", "user.email", "test@example.com", cwd=src)
    _git("config", "user.name", "test", cwd=src)
    for i in range(60):
        (src / f"a{i:02d}.md").write_text(f"# Статья {i}\n", encoding="utf-8")
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", "база", cwd=src)
    return src


def _commit(src, name):
    (src / name).write_text("запись\n", encoding="utf-8")
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", name, cwd=src)


def _snapshot(src, bundle):
    """Как mc-git-bundle.sh на NAS: полный снимок всех веток."""
    bundle.unlink(missing_ok=True)
    assert _git("bundle", "create", str(bundle), "--all", cwd=src).returncode == 0


def _run(tmp_path, bundle, mirror):
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
         "-Bundle", str(bundle), "-Mirror", str(mirror), "-LogFile", str(tmp_path / "mirror.log")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def _packs(mirror):
    return list((mirror / "objects" / "pack").glob("*.pack"))


def test_daily_fetch_does_not_pile_up_full_packs(tmp_path):
    src = _source(tmp_path)
    bundle, mirror = tmp_path / "knowledge-git.bundle", tmp_path / "mirror.git"
    _snapshot(src, bundle)
    _run(tmp_path, bundle, mirror)                     # первый запуск разворачивает зеркало
    for day in range(3):                               # три «суток»: запись и свежий полный снимок
        _commit(src, f"day{day}.md")
        _snapshot(src, bundle)
        _run(tmp_path, bundle, mirror)

    assert len(_packs(mirror)) == 1, "паки копятся после fetch из полного снимка: %d" % len(_packs(mirror))
    # позитивный контроль: один пак не потому, что fetch молча не сработал
    assert _git(f"--git-dir={mirror}", "rev-list", "--count", "HEAD", cwd=tmp_path).stdout.strip() == "4"
    assert "day2.md" in _git(f"--git-dir={mirror}", "ls-tree", "--name-only", "HEAD", cwd=tmp_path).stdout
    assert "OK:" in (tmp_path / "mirror.log").read_text(encoding="utf-8")


def test_rewritten_history_is_not_lost_on_repack(tmp_path):
    """Зеркало — резервная копия. Если историю базы перепишут (как при подмене .git
    26.08.2026), старые объекты станут недостижимыми, и голый repack -a -d их выбросил
    бы — вместе с возможностью достать прежнюю версию файла."""
    src = _source(tmp_path)
    bundle, mirror = tmp_path / "knowledge-git.bundle", tmp_path / "mirror.git"
    _commit(src, "old.md")
    old = _git("rev-parse", "HEAD", cwd=src).stdout.strip()
    _snapshot(src, bundle)
    _run(tmp_path, bundle, mirror)

    _git("reset", "-q", "--hard", "HEAD~1", cwd=src)   # переписанная история: коммита old больше нет
    _commit(src, "new.md")
    _snapshot(src, bundle)
    _run(tmp_path, bundle, mirror)

    assert _git(f"--git-dir={mirror}", "cat-file", "-e", old, cwd=tmp_path).returncode == 0, \
        "переупаковка выбросила объекты переписанной истории"
    assert "new.md" in _git(f"--git-dir={mirror}", "ls-tree", "--name-only", "HEAD", cwd=tmp_path).stdout
