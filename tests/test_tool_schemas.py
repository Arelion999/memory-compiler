"""Инварианты JSON-схем инструментов.

⚠️ Контекст (2026-07-20, замер по живому клиенту). MCP-клиент показывает модели
схему БЕЗ `required` для СТРОКОВЫХ параметров — выживают только object/array.
Сверка «сервер → вид у модели» по шести инструментам, исключений нет:

    save_decision      title,decision,alternatives,reasoning,project → (пусто)
    read_article       project,filename                             → (пусто)
    save_from_template template,fields,project                      → ['fields']
    set_project_deps   project,depends_on                           → ['depends_on']
    save_contexts      project,filename,contexts                    → ['contexts']
    save_tracking      project,entity,facts                         → ['facts']

15 строковых записей выброшено, 4 нестроковых сохранено. Сервер при этом отдаёт
required целиком — потеря ниже по течению, чинить её в этом репозитории нечем.

Отсюда инвариант: **обязательный строковый параметр обязан нести description** —
это единственный канал, который до модели доезжает. Без него модель не знает, что
поле нужно, молча его опускает и получает `expected string, received undefined`.
Ровно так трижды подряд падал save_decision: у его `project` описания не было вовсе.

⚠️ НАЛИЧИЯ description ОКАЗАЛОСЬ МАЛО (замер 2026-08-02, повторный инцидент).
Пять вызовов save_decision подряд упали на `reasoning`, у которого description
БЫЛ — «Почему выбрали это». Текст описывает СМЫСЛ поля и молчит про
обязательность, а модель видит схему без `required` и читает такое поле как
опциональное. Подтверждение из аудита прода: за 2026-08-02 записей save_decision
ноль при 20 записях всего — ни один вызов до сервера не дошёл.

Поэтому маркер обязательности дописывается в description АВТОМАТИЧЕСКИ
(tools._mark_required, на выходе list_tools). Руками у 60 параметров его не
держат: источник правды один — `required` схемы, и на новом инструменте забыть
нельзя. Тесты ниже проверяют обе половины разом — что маркер доехал и что помимо
маркера в описании есть собственный текст.
"""
import asyncio

import pytest

from memory_compiler import i18n, tools
from memory_compiler.tools import list_tools

MARKS = (tools._REQUIRED_MARK_RU, tools._REQUIRED_MARK_EN)


def _required_strings(tool_list):
    """(инструмент.параметр, description) по обязательным СТРОКОВЫМ параметрам."""
    for tool in tool_list:
        schema = tool.inputSchema or {}
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            spec = props.get(name) or {}
            if spec.get("type") == "string":
                yield f"{tool.name}.{name}", spec.get("description") or ""


def test_required_string_params_say_they_are_required():
    """Обязательность сказана СЛОВАМИ, и слова эти не вытеснили смысл поля.

    Одно утверждение ловит оба дефекта: забытый description (останется голый
    маркер) и неработающую маркировку (маркера не будет вовсе).
    """
    unmarked, naked = [], []
    for ref, text in _required_strings(asyncio.run(list_tools())):
        if not text.endswith(MARKS):
            unmarked.append(ref)
            continue
        for mark in MARKS:
            if text.endswith(mark):
                text = text[: -len(mark)]
                break
        if not text.strip():
            naked.append(ref)
    assert not unmarked, (
        "обязательные строковые параметры без маркера обязательности — модель "
        f"прочитает их как опциональные и будет опускать: {unmarked}"
    )
    assert not naked, (
        "у параметра остался один маркер, собственного описания нет — модель "
        f"не поймёт, что писать в поле: {naked}"
    )


def test_required_mark_follows_language(monkeypatch):
    """Маркер идёт на языке MC_LANG, иначе гейт «при en не осталось кириллицы» упадёт."""
    monkeypatch.setattr(i18n, "MC_LANG", "en")
    refs = dict(_required_strings(asyncio.run(list_tools())))
    assert refs["save_decision.reasoning"].endswith(tools._REQUIRED_MARK_EN)

    monkeypatch.setattr(i18n, "MC_LANG", "ru")
    refs = dict(_required_strings(asyncio.run(list_tools())))
    assert refs["save_decision.reasoning"].endswith(tools._REQUIRED_MARK_RU)


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_marking_is_idempotent(monkeypatch, lang):
    """Повторный проход не наслаивает второй маркер.

    list_tools() пересобирает объекты на каждый вызов, так что в бою наслоение
    не грозит; проверка стережёт саму функцию — её могут позвать и отдельно.
    """
    monkeypatch.setattr(i18n, "MC_LANG", lang)
    once = asyncio.run(list_tools())
    before = dict(_required_strings(once))
    after = dict(_required_strings(tools._mark_required(once)))
    assert after == before


def test_save_decision_does_not_require_alternatives():
    """Решение без альтернатив — законный случай, отказывать в записи нельзя.

    Требовать поле, обязательности которого модель не видит, — гарантированный
    отказ на ровном месте.
    """
    tool_map = {t.name: t for t in asyncio.run(list_tools())}
    required = tool_map["save_decision"].inputSchema.get("required") or []
    assert "alternatives" not in required
