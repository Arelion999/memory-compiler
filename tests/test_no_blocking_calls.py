"""Тяжёлые синхронные вызовы не должны выполняться в event loop.

Хендлеры MCP асинхронные. Если async-функция зовёт тяжёлую синхронную операцию
напрямую, event loop встаёт целиком: сервер перестаёт отвечать НА ВСЁ — и на другие
вызовы, и на health. У проекта это уже случалось: search падал в -32001, потому что
дёргал cross-encoder прямо в loop (лечилось asyncio.to_thread, 2026-07-03).

Замер 2026-07-20: `git add -A` на 1815 статьях — 5.5 с локально на SSD, на NAS
заметно дольше. Тринадцать хендлеров звали git_commit синхронно.

Проверка статическая, по AST: вызов внутри async-функции должен быть либо обёрнут
в to_thread, либо не быть в списке тяжёлых. `to_thread(git_commit, msg)` передаёт
функцию как аргумент — это ast.Name, а не ast.Call, поэтому под проверку не попадает.
"""
import ast
from pathlib import Path

import pytest

MC = Path(__file__).resolve().parent.parent / "memory_compiler"

# Функции, которые нельзя звать напрямую из event loop.
# git_commit — subprocess `git add -A` по всей базе знаний (тысячи файлов).
# regenerate_index / rebuild_* — полный обход и перезапись индекса.
HEAVY = {
    "git_commit",
    "regenerate_index",
    "rebuild_index",
    "rebuild_embeddings",
    "whoosh_search",
    "rerank",
}


def blocking_calls(path: Path):
    """Тяжёлые вызовы внутри async-функций, не обёрнутые в to_thread."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    async_fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    found = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", "")
        if name not in HEAVY:
            continue
        owner = None
        for fn in async_fns:
            if fn.lineno <= call.lineno <= fn.end_lineno:
                if owner is None or fn.lineno > owner.lineno:
                    owner = fn
        if owner is None:
            continue  # вызов из синхронной функции — там блокировать нечего
        if "to_thread" in lines[call.lineno - 1]:
            continue
        found.append(f"{path.name}:{call.lineno} в async {owner.name}() → {name}()")
    return found


@pytest.mark.parametrize("module", sorted(p.name for p in MC.glob("*.py")))
def test_no_heavy_calls_in_event_loop(module):
    """Тяжёлая синхронная операция в async-функции обязана уходить в to_thread."""
    found = blocking_calls(MC / module)
    assert not found, (
        "блокируют event loop:\n  " + "\n  ".join(found)
        + "\nОберни в await asyncio.to_thread(...)"
    )
