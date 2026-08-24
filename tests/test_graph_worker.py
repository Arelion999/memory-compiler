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


def strip_comments(js):
    """Убрать //-комментарии: имена глобалов в них законны и не должны ловиться."""
    out = []
    for line in js.split(chr(10)):
        cut = line.find("//")
        out.append(line if cut < 0 else line[:cut])
    return chr(10).join(out)


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


def test_positions_are_interpolated_between_worker_messages():
    """Позиции от воркера сглаживаются, а не применяются рывком.

    Рисуем по requestAnimationFrame (60 Гц), физика тикает по setTimeout — таймеры
    дрейфуют, и без сглаживания кадры то дублируются, то перескакивают через шаг.
    Движение дёргается независимо от того, как быстро считает физика.
    """
    assert "function applyLerp()" in SRC, "нет интерполяции позиций между кадрами воркера"
    assert "function acceptPositions(" in SRC, "нет приёма позиций с запоминанием предыдущих"
    handler = SRC[SRC.index("gWorker.onmessage"):SRC.index("gWorker.postMessage")]
    assert "acceptPositions(" in handler, (
        "обработчик сообщений воркера обязан класть позиции через acceptPositions, "
        "иначе сглаживание обходится стороной")


def test_dragged_node_is_excluded_from_interpolation():
    """Узел под курсором не сглаживается — он обязан идти за рукой без задержки.

    Если его пустить через интерполяцию, он будет отставать от курсора на интервал
    посылки и дёргаться назад к позиции, посчитанной воркером.
    """
    body = extract("applyLerp")
    assert re.search(r"if\s*\(\s*n\s*===\s*gDrag\s*\)\s*continue", body), (
        "applyLerp() должна пропускать перетаскиваемый узел (n===gDrag)")


def test_worker_does_one_step_per_tick():
    """Ровно один шаг физики за тик.

    Прежняя версия догоняла время («сколько успеем за 12 мс, до 4 шагов каждые
    8 мс») и давала до 500 шагов в секунду вместо 60: граф разлетался на глазах.
    Скорость симуляции не должна зависеть от того, как быстро крутится поток.
    """
    body = extract("simWorkerBody")
    code = strip_comments(body)
    assert not re.search(r"while\s*\([^)]*steps\s*<", code), (
        "в тике воркера снова появился цикл «сколько шагов успеем»")
    assert "setTimeout(tick,16)" in code.replace(" ", ""), (
        "тик воркера должен идти с частотой кадра (16 мс)")
