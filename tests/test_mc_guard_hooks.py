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



def _load_stats_hook():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mc_guard_metrics_test", HOOKS / "mc_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hook_search_error_metrics_match_server(monkeypatch, tmp_path, capsys):
    import json
    from datetime import datetime, timedelta
    from memory_compiler import analytics
    hook = _load_stats_hook()
    now = datetime.now()
    def row(sec, tool, size=8000, error=None, **args):
        return {"ts": (now - timedelta(seconds=sec)).strftime(hook.AUDIT_TS_FMT),
                "tool": tool, "size": size, "args": args, **({"error": error} if error else {})}
    rows = [row(150, "search", 0, "TypeError", _count=0),
            row(140, "search", 100, _count=1), row(130, "read_article", error="ValueError"),
            row(120, "save_lesson", project="good"),
            row(110, "search_by_tag", 20), row(100, "ask"),
            row(90, "search_error", 0, "TypeError"), row(80, "read_article"),
            row(70, "finish_task", error="TypeError", project="bad")]
    monkeypatch.setattr(hook, "_tail_audit", lambda **kw: [dict(r) for r in rows])
    monkeypatch.setattr(analytics, "KNOWLEDGE_DIR", tmp_path)
    (tmp_path / "_audit.log").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    actual, expected = hook._product_stats(24), analytics.quality(24)
    assert actual["searches"] == 5
    for key in ("searches", "search_errors", "successful_searches", "acted", "chained", "miss_rate", "act_rate"):
        assert actual[key] == expected[key]
    assert len(actual["misses"]) == expected["misses"] == 1
    assert actual["written"] == expected["writes"] == 1
    assert actual["projects"] == expected["projects"] == [("good", 1)]
    monkeypatch.setattr(hook, "_read_journal", lambda hours: [{"action": "mc.ok", "session": "test"}])
    monkeypatch.setattr(hook, "_pending_all", lambda: [])
    monkeypatch.setattr(hook, "_gate_effect", lambda rows: (0, 0))
    monkeypatch.setattr(sys, "argv", ["mc_guard.py", "stats", "24"])
    assert hook.cmd_stats({}) == 0
    text = capsys.readouterr().out
    assert text.count("1 (33%)") == 2
    assert "доли — от успешных поисков" in text


def test_hook_stats_only_errors_shows_errors_without_ranking_verdict(monkeypatch, capsys):
    from datetime import datetime
    hook = _load_stats_hook()
    rows = [{"ts": datetime.now().strftime(hook.AUDIT_TS_FMT), "tool": "search",
             "size": 0, "args": {"_count": 0}, "error": "TypeError"} for _ in range(12)]
    monkeypatch.setattr(hook, "_tail_audit", lambda **kw: [dict(r) for r in rows])
    monkeypatch.setattr(hook, "_read_journal", lambda hours: [{"action": "mc.ok", "session": "test"}])
    monkeypatch.setattr(hook, "_pending_all", lambda: [])
    monkeypatch.setattr(hook, "_gate_effect", lambda rows: (0, 0))
    monkeypatch.setattr(sys, "argv", ["mc_guard.py", "stats", "24"])
    assert hook.cmd_stats({}) == 0
    text = capsys.readouterr().out
    assert "ошибок поиска" in text and "12" in text
    assert "успешных поисков" in text and "нет" in text
    assert "пробел БАЗЫ" not in text and "стоит посмотреть ранжирование" not in text
