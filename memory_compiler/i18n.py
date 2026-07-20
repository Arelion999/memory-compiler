"""Локализация описаний MCP-инструментов и промптов (MC_LANG).

Постобработка на выходе: tools.py с его 143 русскими описаниями не трогается —
тексты остаются рядом с инструментами, где их видно при чтении кода. Здесь лежит
только английский оверлей, который накладывается в list_tools()/list_prompts().

Границы (осознанные, см. спеку): ответы handlers.py остаются РУССКИМИ, поэтому при
MC_LANG=en клиент увидит английские описания и русский ответ. Лингвистические правила
(regex автотегирования в storage.py, стоп-слова в search.py) не трогаются вовсе —
это обработка русского контента базы, а не интерфейс.
"""
import os

# Дефолт ru — обратная совместимость. Сравнение строгое: любое неизвестное значение
# (fr, опечатка, пустая строка) даёт русский вывод, а не падение.
MC_LANG = os.environ.get("MC_LANG", "ru").lower()

# {имя инструмента: {"description": str, "params": {имя параметра: str}}}
TOOLS_EN: dict[str, dict] = {}

# {имя промпта: {"title": str, "description": str, "args": {имя аргумента: str}}}
PROMPTS_EN: dict[str, dict] = {}


def localize_tools(tools):
    """Английские описания инструментов при MC_LANG=en, иначе вход как есть.

    Возвращает КОПИИ: list_tools() собирает объекты заново, но полагаться на это
    нельзя — мутация чужих объектов сделала бы тесты со сменой языка недостоверными.
    """
    if MC_LANG != "en":
        return tools
    out = []
    for tool in tools:
        entry = TOOLS_EN.get(tool.name)
        if not entry:
            out.append(tool)  # перевода нет — отдаём русский, сервер не падает
            continue
        copy = tool.model_copy(deep=True)
        if entry.get("description"):
            copy.description = entry["description"]
        props = (copy.inputSchema or {}).get("properties", {})
        for param, text in entry.get("params", {}).items():
            if param in props:
                props[param]["description"] = text
        out.append(copy)
    return out


def localize_prompts(prompts):
    """То же для промптов. Переводятся title, description и описания аргументов.

    ⚠️ list_prompts() возвращает МОДУЛЬНУЮ КОНСТАНТУ _PROMPTS — мутация на месте
    испортила бы её на весь процесс.
    """
    if MC_LANG != "en":
        return prompts
    out = []
    for prompt in prompts:
        entry = PROMPTS_EN.get(prompt.name)
        if not entry:
            out.append(prompt)
            continue
        copy = prompt.model_copy(deep=True)
        if entry.get("title"):
            copy.title = entry["title"]
        if entry.get("description"):
            copy.description = entry["description"]
        for arg in copy.arguments or []:
            text = entry.get("args", {}).get(arg.name)
            if text:
                arg.description = text
        out.append(copy)
    return out
