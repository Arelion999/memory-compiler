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


def _all_text(tool):
    """Весь текст инструмента, который видит клиент.

    Схемы обходятся РЕКУРСИВНО и обе — input и output. Плоского обхода properties
    первого уровня недостаточно: у search описание поля uri лежит на глубине
    outputSchema.properties.results.items.properties.uri, и такой текст остался бы
    непроверенным (localize_tools правит только inputSchema).
    """
    def descriptions(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "description" and isinstance(value, str):
                    yield value
                else:
                    yield from descriptions(value)
        elif isinstance(node, list):
            for item in node:
                yield from descriptions(item)

    parts = [tool.description or ""]
    parts += list(descriptions(tool.inputSchema or {}))
    parts += list(descriptions(getattr(tool, "outputSchema", None) or {}))
    return " ".join(parts)


def test_english_output_has_no_cyrillic(monkeypatch):
    """При MC_LANG=en в выводе list_tools() не остаётся кириллицы.

    Проверка ФАКТОМ, а не сверкой ключей: ловит забытый инструмент, забытый параметр
    и недопереведённую строку одним утверждением.

    Зовём list_tools() напрямую, БЕЗ повторного localize_tools: она уже проходит через
    локализацию, и второй вызов проверял бы не тот путь, что работает в бою.
    asyncio.run — конвенция проекта для async в тестах (см. tests/test_ask.py).
    """
    import asyncio

    monkeypatch.setattr(i18n, "MC_LANG", "en")
    from memory_compiler.tools import list_tools

    leftovers = [
        tool.name for tool in asyncio.run(list_tools())
        if CYRILLIC.search(_all_text(tool))
    ]
    assert not leftovers, f"остались русские описания: {leftovers}"


def test_english_prompts_have_no_cyrillic(monkeypatch):
    import asyncio

    monkeypatch.setattr(i18n, "MC_LANG", "en")
    from memory_compiler.tools import list_prompts

    leftovers = []
    for prompt in asyncio.run(list_prompts()):
        text = " ".join(
            [prompt.title or "", prompt.description or ""]
            + [a.description or "" for a in (prompt.arguments or [])]
        )
        if CYRILLIC.search(text):
            leftovers.append(prompt.name)
    assert not leftovers, f"остались русские промпты: {leftovers}"


def test_prompts_constant_not_mutated(monkeypatch):
    """localize_prompts не портит модульную константу _PROMPTS.

    Она общая на весь процесс: мутация на месте сделала бы русский путь недоступным
    после первого английского вызова.
    """
    import asyncio

    monkeypatch.setattr(i18n, "MC_LANG", "en")
    from memory_compiler.tools import _PROMPTS, list_prompts

    asyncio.run(list_prompts())  # английский вызов не должен ничего испортить
    assert CYRILLIC.search(_PROMPTS[0].title or _PROMPTS[0].description or "")


def test_catalog_has_no_stale_keys():
    """В каталоге нет ключей, которых больше нет в коде.

    Ловит удалённый инструмент, чей перевод забыли убрать, — иначе каталог тихо
    копит мусор.
    """
    import asyncio

    from memory_compiler.tools import list_tools

    live = {t.name for t in asyncio.run(list_tools())}
    stale = set(i18n.TOOLS_EN) - live
    assert not stale, f"перевод есть, инструмента нет: {sorted(stale)}"
