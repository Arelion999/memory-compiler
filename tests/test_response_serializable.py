"""Ответы tools обязаны сериализоваться в JSON — иначе клиент не получит ничего.

Регрессия v1.18.0: ответ `reindex` был записан как "\\ud83d\\udd04 Reindex ..." —
это не эмодзи, а ДВА суррогатных code point (Python не склеивает их в литерале).
Такая строка не кодируется в UTF-8, поэтому pydantic падал на model_dump_json:

    PydanticSerializationError: Error serializing to JSON:
    UnicodeEncodeError: 'utf-8' codec can't encode characters in position 0-1:
    surrogates not allowed

Сервер при этом отрабатывал штатно ("tool ok", dur_ms=2, индексы перестраивались),
но SSE-writer падал на отправке — и вызов висел у клиента до таймаута (300 с).
Симптом выглядел как «reindex завис», хотя reindex был ни при чём.

Проверяем ИСХОДНИК, а не только рантайм: строку с суррогатами легко вернуть
копипастом из JSON-дампа, где не-ASCII экранирован в \\uXXXX.
"""
import ast
import pathlib
import re

import pytest

SRC_DIR = pathlib.Path(__file__).resolve().parent.parent / "memory_compiler"

# Суррогаты: D800-DBFF (high) и DC00-DFFF (low). В валидном тексте их быть не может —
# они существуют только как деталь UTF-16 и в Python-строке остаются «висячими».
SURROGATE_ESCAPE = re.compile(r"\\u[dD][89abAB][0-9a-fA-F]{2}")


def _py_sources():
    return sorted(SRC_DIR.rglob("*.py"))


def test_no_surrogate_escapes_in_sources():
    """Ни один .py не должен содержать \\udXXX — такой литерал не сериализуется в JSON."""
    offenders = []
    for path in _py_sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if SURROGATE_ESCAPE.search(line):
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, (
        "суррогатные escape-последовательности (\\udXXX) в исходниках: "
        + ", ".join(offenders)
        + ". Эмодзи вне BMP писать как \\U0001XXXX или самим символом."
    )


def test_all_string_literals_encode_to_utf8():
    """Каждый строковый литерал в пакете обязан кодироваться в UTF-8."""
    bad = []
    for path in _py_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                try:
                    node.value.encode("utf-8")
                except UnicodeEncodeError:
                    bad.append(f"{path.name}:{node.lineno}")
    assert not bad, f"литералы, не кодируемые в UTF-8 (суррогаты?): {bad}"


def test_reindex_response_serializes():
    """Ответ reindex должен пережить model_dump_json — как в реальной отправке по SSE."""
    pytest.importorskip("mcp", reason="нужен MCP SDK — есть в контейнере")
    from mcp.types import TextContent

    from memory_compiler import tools

    src = (SRC_DIR / "tools.py").read_text(encoding="utf-8")
    literals = re.findall(r'text="((?:[^"\\]|\\.)*Reindex(?:[^"\\]|\\.)*)"', src)
    assert literals, "не нашлись ответы reindex в tools.py — тест устарел?"

    for lit in literals:
        value = ast.literal_eval(f'"{lit}"')
        value.encode("utf-8")  # упадёт на суррогатах
        TextContent(type="text", text=value).model_dump_json()

    assert tools is not None
