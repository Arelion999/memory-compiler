"""Скрипты релиза: bash и PowerShell обязаны делать ОДНО И ТО ЖЕ.

Их две реализации одного регламента, и разъезжаются они молча: правишь одну —
вторая продолжает работать по-старому, а заметно это станет на релизе, который
как раз пойдёт через неё.

⚠️ ПОЧЕМУ ТУТ ПРО --config. Правила НИКС лежат ГЛОБАЛЬНО (`~/.git-hooks/gitleaks.toml`),
своего `.gitleaks.toml` в репозитории нет. Без `--config` gitleaks идёт с дефолтными
правилами и без allowlist'ов, и тогда релиз, ТРОГАЮЩИЙ `tests/test_ui_secret_reveal.py`,
отменяется на литерале `test-encrypt-key-123` из `monkeypatch` — на ровном месте,
с формулировкой «найдены секреты». Обычные релизы это не задевало, потому что
исторические фикстуры не попадают в staged, — потому дефект и жил незамеченным.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SH = (SCRIPTS / "release.sh").read_text(encoding="utf-8")
PS1 = (SCRIPTS / "release.ps1").read_text(encoding="utf-8")


@pytest.mark.parametrize("name,text", [("release.sh", SH), ("release.ps1", PS1)])
def test_gitleaks_gets_the_niks_ruleset(name, text):
    """Скан без правил НИКС — это скан не тем, чем проверяет `git audit-secrets`."""
    assert "--config" in text, f"{name}: gitleaks запускается без --config"
    assert ".git-hooks/gitleaks.toml" in text.replace("\\", "/"), (
        f"{name}: не указан глобальный конфиг правил"
    )


@pytest.mark.parametrize("name,text", [("release.sh", SH), ("release.ps1", PS1)])
def test_missing_config_does_not_break_the_scan(name, text):
    """На машине без конфига скан обязан идти с дефолтными правилами, а не падать:
    иначе первый же клон репозитория не сможет выпустить релиз."""
    conditional = ("[ -f " in text) or ("Test-Path" in text)
    assert conditional, f"{name}: --config подставляется безусловно"


def test_both_scripts_still_scan_staged():
    """Область скана — staged. Смена на detect потянула бы историю целиком и
    вернула бы те самые ложные срабатывания."""
    for name, text in (("release.sh", SH), ("release.ps1", PS1)):
        assert re.search(r"gitleaks\s+protect\s+--staged", text), (
            f"{name}: gitleaks больше не сканирует staged"
        )


def test_bash_script_is_valid_syntax():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash не найден — проверка синтаксиса release.sh пропущена")
    done = subprocess.run([bash, "-n", str(SCRIPTS / "release.sh")],
                          capture_output=True, text=True)
    assert done.returncode == 0, f"release.sh невалиден:\n{done.stderr}"


def test_powershell_script_is_valid_syntax():
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("pwsh не найден — проверка синтаксиса release.ps1 пропущена")
    check = (
        "$e=$null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{(SCRIPTS / 'release.ps1').as_posix()}', [ref]$null, [ref]$e) > $null; "
        "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"
    )
    done = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", check],
                          capture_output=True, text=True)
    assert done.returncode == 0, f"release.ps1 невалиден:\n{done.stdout}{done.stderr}"
