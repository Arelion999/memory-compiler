"""Сторожа первого запуска (v1.95.0): решение об офлайн-режиме HF принимается до импорта
ML-библиотек, а умолчания деплоя не возвращают тихую деградацию на свежей установке."""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ML_ROOTS = {"huggingface_hub", "transformers", "sentence_transformers", "torch"}
LATE = {"memory_compiler.tools", "memory_compiler.api", "memory_compiler.search",
        "memory_compiler.handlers", "uvicorn"}


def _imports(tree):
    """(строка, модуль) для каждого import на любой глубине."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module


def _apply_line(tree):
    for node in tree.body:
        call = node.value if isinstance(node, ast.Expr) else None
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "apply" and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "hf_offline"):
            return node.lineno
    return None


def test_server_decides_offline_mode_before_ml_imports():
    """huggingface_hub читает HF_HUB_OFFLINE один раз, при импорте: импорт tools/api выше
    решения заморозил бы режим, и свежая установка снова молча осталась бы без модели."""
    tree = ast.parse((REPO / "server.py").read_text(encoding="utf-8"))
    line = _apply_line(tree)
    assert line is not None, "server.py обязан звать hf_offline.apply() на верхнем уровне"
    early = [(n, m) for n, m in _imports(tree)
             if n < line and (m in LATE or m.split(".")[0] in ML_ROOTS)]
    assert not early, f"импорт до решения об офлайн-режиме: {early}"
    # Позитивный контроль: поздние импорты есть и стоят ниже решения.
    assert any(m == "memory_compiler.tools" and n > line for n, m in _imports(tree))


@pytest.mark.parametrize("module,control", [
    ("hf_offline.py", "memory_compiler.config"), ("config.py", "whoosh.fields"),
])
def test_decision_modules_do_not_import_ml(module, control):
    tree = ast.parse((REPO / "memory_compiler" / module).read_text(encoding="utf-8"))
    mods = {m for _, m in _imports(tree)}
    bad = {m for m in mods if m.split(".")[0] in ML_ROOTS or m in LATE}
    assert not bad, f"{module} импортирует {bad} — это заморозит офлайн-режим до решения"
    assert control in mods, "позитивный контроль: разбор импортов работает"


def _compose_value(name):
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(rf"^\s*-\s*{name}=(.*)$", text, re.M)
    assert m, f"{name} пропал из docker-compose.yml"
    return m.group(1).strip()


@pytest.mark.parametrize("name", ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"])
def test_compose_leaves_offline_mode_to_server(name):
    """Умолчание «1» вернуло бы тихую деградацию на свежей установке, а явное значение из
    .env обязано по-прежнему доезжать до контейнера."""
    assert _compose_value(name) == "${" + name + ":-}"


def test_env_example_describes_auto_mode():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "временно поставь 0" not in text, "ручной шаг первого запуска больше не нужен"
    assert "решает сам" in text and "semantic_reason" in text
