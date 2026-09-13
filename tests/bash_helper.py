"""Рабочий bash для тестов shell-скриптов. Брать ТОЛЬКО через find_bash().

Тесты scripts/*.sh исполняют скрипты по-настоящему. shutil.which("bash") для этого не
годится: из PowerShell первым в PATH стоит C:\\Windows\\System32\\bash.exe — заглушка
WSL, а не bash. Без дистрибутива, где есть /bin/bash (например, когда WSL стоит только
ради Docker Desktop), она падает с `execvpe(/bin/bash) failed`. Так 13.09.2026 полный
прогон из PowerShell дал 17 падений, а из Git Bash тот же набор проходил целиком.

Порядок на Windows: <Git>\\bin\\bash.exe из Git for Windows, затем все bash из PATH.
Именно bin\\, а не соседний usr\\bin\\bash.exe: тот, запущенный не из Git Bash, остаётся
без /usr/bin в PATH — date и sha1sum не находятся, а find, sort и tar берутся
виндовские (замер 13.09.2026). Обёртка из bin\\ сама ставит PATH и MSYSTEM.
Вне Windows — первый bash из PATH, как и раньше.

Кандидат принимается только после пробы, и `bash -c 'echo ok'` для неё мало: WSL с
настоящим дистрибутивом его пройдёт. А тесты передают скрипт путём хоста и настройки
переменными окружения — пути C:\\… внутри Linux нет, переменные без WSLENV не едут.
Поэтому проба делает то же, что тесты: запускает файл по пути хоста, ждёт маркер из
окружения и заодно проверяет, что find и date настоящие.

Рабочего bash нет — тест пропускается, и в причине перечислено, что отвергнуто и почему.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, NamedTuple

PROBE_TIMEOUT = 30          # холодный старт WSL — секунды; зависший кандидат не вешает прогон
_MARKER = "bash-probe-ok"
# LF обязателен: на CRLF bash спотыкается о \r раньше, чем о настоящую проблему.
_PROBE_SCRIPT = (
    'find . -maxdepth 0 >/dev/null 2>&1'
    ' || { echo "find не POSIX: первым в PATH стоит виндовский find.exe?" >&2; exit 3; }\n'
    'date +%s >/dev/null 2>&1 || { echo "нет date в PATH" >&2; exit 3; }\n'
    'printf "%s" "$MC_BASH_PROBE"\n'
)


class Bash(NamedTuple):
    path: str | None    # None — рабочего bash нет
    reason: str         # почему нет; пусто, когда найден


def probe(bash: str) -> str | None:
    """None — bash рабочий; иначе строка: какой bash и почему не годится."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        script = Path(tmp) / "probe.sh"
        script.write_bytes(_PROBE_SCRIPT.encode("utf-8"))
        env = dict(os.environ, MC_BASH_PROBE=_MARKER)
        try:
            done = subprocess.run([bash, str(script)], env=env,
                                  capture_output=True, timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            return "%s: %s" % (bash, e)
    if done.returncode == 0 and done.stdout == _MARKER.encode():
        return None
    # wsl.exe пишет часть сообщений в UTF-16: без выброса нулей причина не читается.
    text = (done.stderr + done.stdout).decode("utf-8", "replace").replace("\x00", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "%s: %s" % (bash, lines[-1] if lines else
                       "код выхода %d, маркер не получен" % done.returncode)


def _git_for_windows() -> list[str]:
    """<Git>\\bin\\bash.exe: вверх от git из PATH, затем обычные места установки."""
    roots: list[Path] = []
    git = shutil.which("git")
    if git:
        roots.extend(Path(git).resolve().parents)
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]) / "Git")
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git")
    return [str(r / "bin" / "bash.exe") for r in roots if (r / "bin" / "bash.exe").is_file()]


def candidates() -> list[str]:
    """Кого пробовать, по порядку. Вне Windows — ровно прежний shutil.which."""
    if os.name != "nt":
        found = shutil.which("bash")
        return [found] if found else []
    found = _git_for_windows() + [b for b in (shutil.which("bash", path=d)
                                               for d in os.get_exec_path()) if b]
    unique: dict[str, str] = {}
    for b in found:
        unique.setdefault(os.path.normcase(os.path.abspath(b)), b)
    return list(unique.values())


def pick(cands: Iterable[str]) -> Bash:
    """Первый кандидат, прошедший пробу; иначе — причина со всеми отказами."""
    rejected = []
    for bash in cands:
        why = probe(bash)
        if why is None:
            return Bash(bash, "")
        rejected.append(why)
    if not rejected:
        where = "в Git for Windows и в PATH" if os.name == "nt" else "в PATH"
        return Bash(None, "рабочий bash не найден: ни одного bash %s" % where)
    return Bash(None, "рабочий bash не найден, отвергнуты — " + "; ".join(rejected))


@functools.lru_cache(maxsize=None)
def find_bash() -> Bash:
    """Рабочий bash для тестов скриптов; проба одна на прогон.

        BASH, NO_BASH = find_bash()
        pytestmark = pytest.mark.skipif(BASH is None, reason=NO_BASH)
    """
    return pick(candidates())
