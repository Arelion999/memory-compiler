"""Поиск рабочего bash для тестов shell-скриптов (tests/bash_helper.py).

Живой случай 13.09.2026: полный прогон из PowerShell дал 17 падений в тестах
scripts/*.sh с одной и той же ошибкой `execvpe(/bin/bash) failed`. shutil.which("bash")
вернул C:\\Windows\\System32\\bash.exe — заглушку WSL, а из Git Bash тот же набор
проходил целиком. Тесты ниже держат три вещи: заглушку за bash не принимаем; рабочего
bash нет — пропуск с названной причиной; там, где Git Bash есть, скрипты реально
исполняются, а не скипаются.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.bash_helper import find_bash, pick, probe

TESTS = Path(__file__).resolve().parent


def _git_root():
    """Корень Git for Windows — через `git --exec-path`, а НЕ так, как ищет хелпер
    (тот идёт вверх от git.exe): общая ошибка поиска не спрячется в обоих местах."""
    git = shutil.which("git")
    if git is None:
        return None
    out = subprocess.run([git, "--exec-path"], capture_output=True, text=True).stdout.strip()
    parents = Path(out).parents if out else ()
    return parents[2] if len(parents) > 2 else None   # <Git>/mingw64/libexec/git-core


def test_missing_candidate_is_rejected_with_its_path(tmp_path):
    missing = str(tmp_path / "нет-такого-bash")
    found = pick([missing])
    assert found.path is None
    assert missing in found.reason, "причина обязана назвать, какой bash отвергнут"


def test_candidate_that_cannot_run_a_script_is_rejected():
    """Исполняемый файл есть, а скрипт не выполняет — ровно как заглушка WSL.

    Python на месте bash: проба для него — синтаксическая ошибка с кодом 1.
    """
    why = probe(sys.executable)
    assert why is not None, "python принят за рабочий bash"
    assert sys.executable in why


def test_broken_candidates_are_passed_over_for_a_working_one(tmp_path):
    real = find_bash().path
    if real is None:
        pytest.skip("рабочего bash на машине нет — нечем проверить выбор следующего")
    found = pick([str(tmp_path / "нет-такого-bash"), sys.executable, real])
    assert found.path == real


def test_no_working_bash_gives_explicit_reason():
    assert pick([]).path is None
    assert pick([]).reason.startswith("рабочий bash не найден")
    found = pick([sys.executable])
    assert found.path is None
    assert found.reason.startswith("рабочий bash не найден")
    assert sys.executable in found.reason, "из причины должно быть видно, что пробовали"


@pytest.mark.skipif(os.name != "nt", reason="заглушка WSL бывает только в Windows")
def test_wsl_stub_is_not_a_working_bash():
    """Тот самый bash, что уронил 17 тестов. С установленным дистрибутивом проба тоже
    не пройдёт: пути C:\\… внутри Linux нет, а переменные окружения без WSLENV не едут."""
    stub = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "bash.exe"
    if not stub.is_file():
        pytest.skip("заглушки WSL на машине нет")
    assert probe(str(stub)) is not None, "заглушка WSL принята за рабочий bash"


@pytest.mark.skipif(os.name != "nt", reason="Git for Windows бывает только в Windows")
def test_msys_bash_without_usr_bin_in_path_is_rejected(monkeypatch):
    """<Git>\\usr\\bin\\bash.exe вне Git Bash остаётся без /usr/bin в PATH: date нет,
    find виндовский (замер 13.09.2026). Скрипты на таком bash падают не сразу и не
    там, где стоит искать. Обёртка <Git>\\bin\\bash.exe при том же PATH рабочая."""
    root = _git_root()
    if root is None or not (root / "usr" / "bin" / "bash.exe").is_file():
        pytest.skip("Git for Windows с bash не установлен")
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PATH", os.pathsep.join([os.path.join(windir, "System32"), windir]))
    assert probe(str(root / "usr" / "bin" / "bash.exe")) is not None, (
        "usr\\bin\\bash.exe без /usr/bin в PATH принят за рабочий")
    assert probe(str(root / "bin" / "bash.exe")) is None


@pytest.mark.skipif(os.name != "nt", reason="Git for Windows бывает только в Windows")
def test_git_bash_is_found_where_installed():
    """Позитивный контроль: там, где Git Bash есть, тесты скриптов ИСПОЛНЯЮТСЯ.

    Иначе починка выродилась бы в тихий скип: 17 тестов «не падают», и ни один
    скрипт не запущен.
    """
    root = _git_root()
    if root is None or not (root / "bin" / "bash.exe").is_file():
        pytest.skip("Git for Windows с bash не установлен")
    found = find_bash()
    assert found.path is not None, found.reason
    assert os.path.normcase(found.path) == os.path.normcase(str(root / "bin" / "bash.exe")), (
        "взят не bash из Git for Windows: %s" % found.path)


@pytest.mark.skipif(os.name == "nt", reason="проверка поведения вне Windows")
def test_outside_windows_bash_comes_from_path_as_before():
    """Вне Windows ничего не меняется: тот же bash, что давал shutil.which."""
    expected = shutil.which("bash")
    if expected is None:
        pytest.skip("bash не в PATH")
    assert find_bash().path == expected


def test_shell_script_tests_take_bash_from_helper():
    """Сторож: тест скрипта, который снова возьмёт shutil.which("bash") или голое
    "bash", из PowerShell получит заглушку WSL. Голое имя не спасает и из Git Bash:
    CreateProcess ищет в System32 раньше, чем в PATH."""
    pattern = re.compile(r"""which\(\s*["']bash["']|\[\s*["']bash["']\s*,""")
    offenders = []
    for f in sorted(TESTS.glob("*.py")):
        if f.name in ("bash_helper.py", "test_bash_helper.py"):
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append("%s:%d" % (f.name, n))
    assert not offenders, "bash берётся мимо tests/bash_helper.find_bash: " + ", ".join(offenders)
