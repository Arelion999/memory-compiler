"""scripts/model_side_usage.py: расход со стороны модели по транскриптам Claude Code."""
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "model_side_usage.py"
NOW = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
P = "mcp__memory-compiler__"
LINK = "[Resource link: p/a.md] memory://p/a.md (score: 90)"
HIT = json.dumps({"query": "q", "count": 1, "results": [{"project": "p", "file": "a.md"}]})
EMPTY = json.dumps({"query": "z", "count": 0, "results": []})


def _load():
    spec = importlib.util.spec_from_file_location("model_side_usage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assistant(mid, tid, tool, inp, usage=None, sidechain=False):
    return {"type": "assistant", "timestamp": NOW, "isSidechain": sidechain, "message": {"id": mid, "usage": usage or {},
            "content": [{"type": "tool_use", "id": tid, "name": P + tool, "input": inp}]}}


def _result(tid, *texts, sidechain=False):
    return {"type": "user", "timestamp": NOW, "isSidechain": sidechain, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": t} for t in texts]}]}}


def _session():
    return [
        _assistant("m1", "t1", "search", {"query": "q"}),
        _result("t1", LINK, HIT),
        _assistant("m2", "t2", "read_article", {"project": "p", "filename": "a.md"}, {"output_tokens": 0}),
        _result("t2", "x" * 4000),
        _assistant("m3", "t3", "search", {"query": "z"},
                   {"input_tokens": 10, "cache_creation_input_tokens": 2600,
                    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 2600},
                    "cache_read_input_tokens": 2000, "output_tokens": 5}),
        _result("t3", EMPTY),
        _assistant("m4", "t4", "search", {"query": "z"}),
        _result("t4", EMPTY),
        _result("t4", EMPTY, sidechain=True),
        _assistant("sm1", "s1", "search", {"query": "subagent"}, sidechain=True),
        _result("s1", EMPTY, sidechain=True),
    ]


def _write(path, lines):
    path.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
    return str(path)


def test_counts_what_the_model_received(tmp_path):
    report = _load().measure([_write(tmp_path / "s.jsonl", _session())])
    assert report["tools"]["search"]["n"] == 3
    assert report["tools"]["read_article"]["n"] == 1
    s = report["search"]
    assert s["link_chars"] == len(LINK)
    assert s["json_chars"] == len(HIT) + 2 * len(EMPTY)
    assert s["followed_by_read"] == 1, "t1 → чтение своего результата"
    assert s["followed_by_search"] == 1, "t3 → сразу t4"


def test_resumed_session_copies_are_not_double_counted(tmp_path):
    lines = _session()
    report = _load().measure([_write(tmp_path / "a.jsonl", lines), _write(tmp_path / "b.jsonl", lines)])
    assert report["tools"]["search"]["n"] == 3


def test_tokens_per_char_comes_from_the_turn_after_a_big_result(tmp_path):
    report = _load().measure([_write(tmp_path / "s.jsonl", _session())])
    assert report["tokens_per_char"] == pytest.approx(2610 / 4000)
    assert report["token_eq"] == pytest.approx(10 + 2600 * 2.0 + 2000 * 0.1 + 5 * 5), "часовая запись кэша ×2"


def test_cache_write_without_ttl_split_is_priced_as_five_minutes(tmp_path):
    lines = [_assistant("m1", "t1", "search", {"query": "q"}, {"cache_creation_input_tokens": 100}),
             _result("t1", EMPTY)]
    report = _load().measure([_write(tmp_path / "s.jsonl", lines)])
    assert report["token_eq"] == pytest.approx(125)


def test_cli_prints_the_shares(tmp_path, capsys):
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    _write(root / "s.jsonl", _session())
    assert _load().main(["--root", str(tmp_path / "projects"), "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "поиск -> чтение своего результата: 33.3%" in out
    assert "поиск -> сразу поиск: 33.3%" in out


def test_sidechain_results_are_excluded(tmp_path):
    """Проверяем, что результаты от сабагентов (isSidechain=True) не считаются."""
    report = _load().measure([_write(tmp_path / "s.jsonl", _session())])
    # Всё ещё 3 поиска, несмотря на сидчейн поиск s1
    assert report["tools"]["search"]["n"] == 3
    # json_chars остаются без одного EMPTY от сидчейна s1
    assert report["search"]["json_chars"] == len(HIT) + 2 * len(EMPTY)


def test_script_runs_without_pythonioencoding_workaround(tmp_path):
    """Скрипт печатает ≈ без UnicodeEncodeError на консоли cp1251."""
    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    _write(root / "s.jsonl", _session())
    env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path / "projects"), "--days", "1"],
        env=env,
        capture_output=True,
        timeout=30
    )
    assert result.returncode == 0, f"Script failed: {result.stderr.decode('utf-8', errors='replace')}"
    out = result.stdout.decode("utf-8", errors="replace")
    assert "поиск -> чтение своего результата: 33.3%" in out
