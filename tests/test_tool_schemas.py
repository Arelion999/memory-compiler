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


# ── `default` в схеме отбивает вызов у клиента (v1.90.0) ────────────────────
# Замер 20.09.2026 по транскриптам: 78 отказов `-32602` по инструментам базы за
# 30 дней, и 18 из них — на параметрах, которые модель И НЕ ДОЛЖНА заполнять:
#
#     save_lesson(content, project, tags, topic) → «expected nonoptional,
#                                                   received undefined: force_new»
#     route_project(text, cwd)                   → то же про top_k
#     search_by_tag(tag)                         → то же про project
#
# Все эти параметры необязательные и несут `default` в схеме. Клиент строит из
# неё zod и требует их явно — вызов не доходит до сервера, запись теряется.
# Значение по умолчанию сервер и так подставляет сигнатурой, поэтому `default`
# в схеме не нужен; чтобы модель о нём знала, оно называется в description.

def _params_with_default(tool_list):
    for tool in tool_list:
        for name, spec in ((tool.inputSchema or {}).get("properties") or {}).items():
            if isinstance(spec, dict) and "default" in spec:
                yield "%s.%s" % (tool.name, name)


def test_schemas_carry_no_default():
    offenders = sorted(_params_with_default(asyncio.run(list_tools())))
    assert offenders == [], (
        "`default` в схеме заставляет клиента требовать параметр явно: %s" % offenders)


def test_optional_params_still_name_their_default_value():
    """Позитивный контроль: убрав `default`, нельзя смолчать о его значении —
    иначе модель перестанет знать, что `project` по умолчанию «all»."""
    tools_by_name = {t.name: t for t in asyncio.run(list_tools())}
    for tool_name, param, expected in (("search", "project", "all"),
                                       ("search_by_tag", "project", "all"),
                                       ("route_project", "top_k", "3"),
                                       ("save_lesson", "force_new", "false"),
                                       ("save_tracking", "replace", "false")):
        spec = ((tools_by_name[tool_name].inputSchema or {}).get("properties") or {})[param]
        text = (spec.get("description") or "").lower()
        assert expected in text, (
            "%s.%s не называет значение по умолчанию: %r" % (tool_name, param, text))
