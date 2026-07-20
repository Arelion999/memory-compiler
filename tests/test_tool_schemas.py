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
"""
import asyncio

from memory_compiler.tools import list_tools


def test_required_string_params_carry_description():
    """Обязательность строкового поля должна быть сказана словами, а не только в required."""
    naked = []
    for tool in asyncio.run(list_tools()):
        schema = tool.inputSchema or {}
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            spec = props.get(name) or {}
            if spec.get("type") == "string" and not spec.get("description"):
                naked.append(f"{tool.name}.{name}")
    assert not naked, (
        "обязательные строковые параметры без description — модель не увидит "
        f"их обязательности и будет их опускать: {naked}"
    )


def test_save_decision_does_not_require_alternatives():
    """Решение без альтернатив — законный случай, отказывать в записи нельзя.

    Требовать поле, обязательности которого модель не видит, — гарантированный
    отказ на ровном месте.
    """
    tools = {t.name: t for t in asyncio.run(list_tools())}
    required = tools["save_decision"].inputSchema.get("required") or []
    assert "alternatives" not in required
