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


# ─── Проверки ОТРЕНДЕРЕННОЙ страницы ────────────────────────────────────────
# Всё выше разбирает ui.py как исходник. Этого мало: страница уезжает клиенту ПОСЛЕ
# подстановок в api.py, и сломаться она может именно там. Так и произошло — комментарий
# в JS упоминал имя плейсхолдера буквально, str.replace подставил на это место
# многострочный CSS, тот влился в однострочный //-комментарий, и весь скрипт умер с
# «Unexpected token '{'». Исходник при этом был синтаксически безупречен.

PLACEHOLDERS = {
    # плейсхолдер: сколько раз он ДОЛЖЕН встречаться во всём ui.py
    "/*PYGMENTS_CSS*/": 1,   # одно место подстановки, в <style> главной страницы
    "/*MC_LANG*/": 2,        # два: WEB_HTML и LOGIN_HTML
}


@pytest.mark.parametrize("placeholder,expected", sorted(PLACEHOLDERS.items()))
def test_placeholder_not_mentioned_anywhere_else(placeholder, expected):
    """Имя плейсхолдера встречается РОВНО в местах подстановки — и нигде больше.

    Подстановка делается str.replace по всему документу, поэтому любое упоминание
    (хоть в комментарии, хоть в тексте подписи) станет точкой вставки. Описывай
    механизм словами, не литералом.
    """
    found = SRC.count(placeholder)
    assert found == expected, (
        f"{placeholder}: найдено {found}, ожидалось {expected}. "
        f"Лишнее упоминание станет точкой подстановки и сломает страницу."
    )


def rendered_js(template, lang="ru"):
    """JS страницы в том виде, в каком его получает браузер — после всех подстановок."""
    from memory_compiler.markdown_render import pygments_css

    html = template.replace("/*PYGMENTS_CSS*/", pygments_css()).replace("/*MC_LANG*/", lang)
    return "".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S))


@pytest.mark.parametrize("page", ["WEB_HTML", "LOGIN_HTML"])
def test_rendered_js_is_valid_syntax(page, tmp_path):
    """JS отрендеренной страницы синтаксически валиден.

    Проверяем ПОСЛЕ подстановок: исходник может быть корректным, а результат — нет.
    Требует node; без него пропускаем, чтобы тест не падал на машине без него.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен — проверка синтаксиса отрендеренного JS пропущена")

    from memory_compiler import ui

    js_file = tmp_path / f"{page}.js"
    js_file.write_text(rendered_js(getattr(ui, page)), encoding="utf-8")
    done = subprocess.run([node, "--check", str(js_file)], capture_output=True, text=True)
    assert done.returncode == 0, f"{page}: JS отрендеренной страницы невалиден:\n{done.stderr}"
