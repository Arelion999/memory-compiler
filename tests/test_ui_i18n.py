"""Полнота словаря Web UI.

ui.py — это Python-строка с HTML внутри, а не импортируемый модуль, поэтому тест
разбирает её как ТЕКСТ. Проверка по факту, а не сверкой списков: главная —
test_no_untranslated_strings, потому что сверка ключей между собой не заметит
подпись, которую просто не пометили.
"""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "memory_compiler" / "ui.py"
SRC = UI.read_text(encoding="utf-8")
CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")


def scripts_body():
    """Тело <script> БЕЗ словарей переводов: там русский законен, это его место.

    Словари обрамлены маркерами /* i18n-dict */ … /* /i18n-dict */. Маркеры, а не
    имя переменной: словарей ДВА — общий I18N в WEB_HTML и маленький L на странице
    логина. Поиск по `var I18N=` пропустил бы второй, и тест упал бы на легитимном
    «Вход»/«Войти».
    """
    js = "".join(re.findall(r"<script[^>]*>(.*?)</script>", SRC, re.S))
    return re.sub(r"/\* i18n-dict \*/.*?/\* /i18n-dict \*/", "", js, flags=re.S)


def dict_keys(lang):
    """Ключи одного языка из словаря I18N (главная страница)."""
    js = "".join(re.findall(r"<script[^>]*>(.*?)</script>", SRC, re.S))
    start = js.find("var I18N=")
    block = js[start:js.find("};", start)]
    section = re.search(rf"{lang}:\s*{{(.*?)}}", block, re.S)
    assert section, f"в I18N нет секции {lang}"
    return set(re.findall(r'"([^"]+)"\s*:', section.group(1)))


def used_keys():
    """Ключи, которые реально используются: data-i18n* в разметке и t('…') в коде."""
    from_attrs = set(re.findall(r'data-i18n(?:-ph|-title)?="([^"]+)"', SRC))
    from_calls = set(re.findall(r"""\bt\(\s*["']([^"']+)["']\s*\)""", SRC))
    return from_attrs | from_calls


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_used_key_exists(lang):
    """Каждый используемый ключ есть в обоих языках. Ловит опечатку и забытый перевод."""
    missing = sorted(used_keys() - dict_keys(lang))
    assert not missing, f"нет перевода на {lang}: {missing}"


def test_no_stale_keys():
    """В словаре нет ключей, которых нет в разметке — иначе он копит мусор."""
    stale = sorted(dict_keys("ru") - used_keys())
    assert not stale, f"ключ есть, использования нет: {stale}"


def test_no_untranslated_strings():
    """В коде JS не осталось русских литералов вне словарей.

    Комментарии (// до конца строки) исключаются: около 20 строк вроде «защита от
    зацикливания» — это код, а не интерфейс, переводить их незачем.
    """
    bad = []
    for line in scripts_body().splitlines():
        code, _, _ = line.strip().partition("//")
        for lit in re.findall(r"""["'`]([^"'`]*[а-яёА-ЯЁ][^"'`]*)["'`]""", code):
            if lit.strip():
                bad.append(lit.strip()[:60])
    assert not bad, f"русские литералы вне словаря: {bad}"


def test_placeholder_present():
    """Плейсхолдер языка есть в обеих страницах — иначе подстановка не сработает."""
    from memory_compiler.ui import WEB_HTML, LOGIN_HTML

    assert "/*MC_LANG*/" in WEB_HTML, "нет /*MC_LANG*/ в WEB_HTML"
    assert "/*MC_LANG*/" in LOGIN_HTML, "нет /*MC_LANG*/ в LOGIN_HTML"
