"""Заготовка каталога переводов: извлекает ключи и русские тексты из tools.py.

Печатает готовый python-литерал в stdout. Переводы вписываются поверх русских
текстов вручную — генератор ничего не переводит, он только гарантирует, что
ни один инструмент и ни один параметр не потеряется.

Запуск: python scripts/gen_i18n_stub.py > /tmp/stub.py
"""
import ast
import pathlib
import re
import sys

CYR = re.compile(r"[а-яёА-ЯЁ]")
SRC = pathlib.Path(__file__).resolve().parent.parent / "memory_compiler" / "tools.py"


def main():
    # На Windows stdout при перенаправлении в файл иногда открывается в кодировке
    # консоли (cp1251), а не UTF-8 — кириллица превращается в "?", вывод падает
    # на первом символе вне cp1251 (например, "→"). Принудительно фиксируем UTF-8,
    # чтобы скрипт работал одинаково из Git Bash/PowerShell и на NAS.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    print("TOOLS_EN = {")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Tool"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        name = kw["name"].value
        desc = kw["description"].value if isinstance(kw.get("description"), ast.Constant) else ""
        print(f"    {name!r}: {{")
        print(f"        # RU: {desc}")
        print(f"        'description': {desc!r},")
        params = []
        schema = kw.get("inputSchema")
        if isinstance(schema, ast.Dict):
            for k, v in zip(schema.keys, schema.values):
                if getattr(k, "value", None) != "properties" or not isinstance(v, ast.Dict):
                    continue
                for pk, pv in zip(v.keys, v.values):
                    if not isinstance(pv, ast.Dict):
                        continue
                    for dk, dv in zip(pv.keys, pv.values):
                        if getattr(dk, "value", None) == "description" and isinstance(dv, ast.Constant):
                            if CYR.search(str(dv.value)):
                                params.append((pk.value, dv.value))
        if params:
            print("        'params': {")
            for p, t in params:
                print(f"            {p!r}: {t!r},")
            print("        },")
        print("    },")
    print("}")


if __name__ == "__main__":
    main()
