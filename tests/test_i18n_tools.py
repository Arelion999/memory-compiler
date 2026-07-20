"""MC_LANG: английские описания инструментов.

Полнота каталога проверяется ФАКТОМ, а не сверкой ключей: при MC_LANG=en в выводе
не должно остаться ни одной кириллической буквы. Одно утверждение ловит забытый
инструмент, забытый параметр и недопереведённую строку.
"""
import re

import pytest

from memory_compiler import i18n

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")


def test_russian_path_returns_same_object(monkeypatch):
    """При русском языке список не копируется и не меняется.

    Проверяем тождеством, а не сравнением текстов: снапшот 143 описаний пришлось бы
    править при каждой правке формулировки, и он стал бы дубликатом tools.py.
    """
    monkeypatch.setattr(i18n, "MC_LANG", "ru")
    sentinel = ["не список Tool, и это нормально — функция не должна его трогать"]
    assert i18n.localize_tools(sentinel) is sentinel
    assert i18n.localize_prompts(sentinel) is sentinel


@pytest.mark.parametrize("value", ["fr", "EN_US", "", "ru"])
def test_unknown_language_falls_back_to_russian(monkeypatch, value):
    """Любое значение кроме 'en' — русский путь, без падения."""
    monkeypatch.setattr(i18n, "MC_LANG", value)
    sentinel = ["x"]
    assert i18n.localize_tools(sentinel) is sentinel
