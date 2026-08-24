"""Физика графа уезжает в Web Worker — её исходник обязан быть самодостаточным.

Код воркера собирается в браузере как simCore.toString() + simWorkerBody.toString().
Если внутри появится обращение к переменной страницы (gNodes, G_REP, document…),
внутри потока это станет ReferenceError, который НИКТО не увидит: воркер просто
перестанет слать позиции, граф замрёт без единого сообщения в консоли страницы.
Отсюда сторож — он проверяет ровно эту самодостаточность.
"""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "memory_compiler" / "ui.py"
SRC = UI.read_text(encoding="utf-8")

# Идентификаторы страницы, которых в коде воркера быть не должно.
PAGE_GLOBALS = re.compile(
    r"\b(?:gNodes|gEdges|gAdj|gMap|gSim|gWorker|gAlpha|gZoom|gCamX|gCamY|gDim|gFade|"
    r"gHover|gDrag|gCtx|gW|gH|gDpr|gFollow|gHi|gPulse|gSpark|gGlowR|"
    r"G_REP|G_LEN|G_THETA|G_DECAY|G_VDECAY|G_HEAVY|G_ALPHA_MIN|"
    r"document|window|localStorage|requestAnimationFrame|renderGraph|graphWake)\b"
)

# Разрешённое окружение воркера: то, что есть и в потоке, и на странице.
WORKER_OK = {"Math", "Date", "Float64Array", "Int32Array", "Infinity", "postMessage",
             "onmessage", "setTimeout", "clearTimeout", "simCore", "simWorkerBody"}


def extract(name):
    """Тело функции верхнего уровня из ui.py — по балансу фигурных скобок.

    Регуляркой «до первой }» тут не обойтись: внутри десятки вложенных блоков.
    """
    start = SRC.index(f"function {name}(")
    i = SRC.index("{", start)
    depth, j = 0, i
    while j < len(SRC):
        c = SRC[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return SRC[start:j + 1]
        j += 1
    raise AssertionError(f"не нашёл тело функции {name}")


@pytest.mark.parametrize("name", ["simCore", "simWorkerBody"])
def test_worker_code_is_self_contained(name):
    """В коде, уезжающем в воркер, нет обращений к переменным страницы."""
    body = extract(name)
    # комментарии не код: в них имена глобалов упоминать можно и нужно
    code = re.sub(r"//[^\n]*", "", body)
    found = sorted(set(PAGE_GLOBALS.findall(code)))
    assert not found, (
        f"{name}() ссылается на окружение страницы: {found}. "
        "В Web Worker это ReferenceError, который не виден в консоли страницы — "
        "граф просто замрёт. Передавай значения через объект состояния."
    )


def test_worker_source_is_assembled_from_those_functions():
    """Сборка исходника воркера идёт именно из этих двух функций.

    Если сборку перепишут (например, начнут склеивать строку руками), сторож выше
    начнёт проверять код, который в воркер уже не уезжает.
    """
    assert "simCore.toString()" in SRC, "исходник воркера собирается не из simCore"
    assert "simWorkerBody.toString()" in SRC, "исходник воркера собирается не из simWorkerBody"
    assert "new Worker(" in SRC, "воркер не создаётся"


def test_no_backslash_escape_in_worker_glue():
    """Склейка не использует backslash-n.

    ui.py — ОБЫЧНАЯ Python-строка, и литерал с backslash-n в ней превращается в
    настоящий перевод строки, разрывая JS-литерал. На этом уже один раз падала
    сборка исходника воркера, поэтому склейка идёт через fromCharCode.
    """
    glue_start = SRC.index("simCore.toString()")
    glue = SRC[glue_start - 200:glue_start + 300]
    assert "String.fromCharCode(10)" in glue, (
        "склейка исходника воркера должна использовать String.fromCharCode(10)")


def test_fallback_path_exists():
    """Есть запасной синхронный путь: без Worker граф обязан работать."""
    assert "gWorkerOff" in SRC, "нет флага отключения воркера"
    assert re.search(r"function simStep\(\)\s*\{[^}]*simCore\(", SRC, re.S), (
        "запасной simStep() должен звать тот же simCore")
