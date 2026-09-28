# -*- coding: utf-8 -*-
"""Прогон hooks/test_mc_guard.py под обоими клиентскими профилями + гейт приватности.

Сьют хука — самостоятельный скрипт (hooks/test_mc_guard.py), профиль задаётся
MC_GUARD_CLIENT. Здесь он гоняется дважды: claude и kimi. Гейт приватности
страхует публичный репозиторий: в hooks/ не должно быть литералов с данными
машины владельца (IP, пути, имя пользователя) — боевые значения живут только
в локальном mc_guard.env, который не коммитится.
"""
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SUITE = HOOKS / "test_mc_guard.py"

# Приватные литералы, которым не место в публичном репо (нижний регистр).
_PRIVATE = ("192.168.", "synologydrive", "areli")


def _run_suite(client):
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["MC_GUARD_CLIENT"] = client
    return subprocess.run(
        [sys.executable, str(SUITE)], capture_output=True, timeout=600, env=env)


def test_suite_profile_claude():
    proc = _run_suite("claude")
    assert proc.returncode == 0, proc.stdout.decode("utf-8", errors="replace")[-2000:]


def test_suite_profile_kimi():
    proc = _run_suite("kimi")
    assert proc.returncode == 0, proc.stdout.decode("utf-8", errors="replace")[-2000:]


def test_hooks_sources_have_no_private_literals():
    bad = []
    for path in sorted(HOOKS.glob("*.py")) + sorted(HOOKS.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for needle in _PRIVATE:
            if needle in text:
                bad.append("%s: %s" % (path.name, needle))
    assert not bad, "приватные литералы в hooks/: %s" % ", ".join(bad)
