"""POST /api/reflex — памятки для хука клиента (v1.78.0)."""
import asyncio
import json

from memory_compiler import reflexes
from memory_compiler.api import web_reflex


class JsonRequest:
    """Стенд под starlette.Request: web_reflex читает только await request.json()."""

    def __init__(self, payload=None, raw=None):
        self._payload, self._raw = payload, raw

    async def json(self):
        return json.loads(self._raw) if self._raw is not None else self._payload


def _call(req):
    resp = asyncio.run(web_reflex(req))
    return resp.status_code, json.loads(resp.body)


def test_reflex_endpoint_returns_memos_and_text(knowledge_dir, monkeypatch):
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    (knowledge_dir / "testproj" / "py.md").write_text(
        "# Python на Windows не находится\n\n**Дата:** 2026-04-12 10:00\n\n## Записи\n\n"
        "### 2026-04-12 10:00\nЗвать python, а не python3.\n\n## Рефлексы\n"
        "- ошибка: Python was not found; run without arguments to install from the Microsoft Store\n",
        encoding="utf-8")
    code, data = _call(JsonRequest({
        "kind": "error", "cwd": "C:/x/testproj",
        "text": "Exit code 49\nPython was not found; run without arguments to install "
                "from the Microsoft Store, or disable this shortcut"}))
    assert code == 200
    assert [(m["file"], m["via"]) for m in data["memos"]] == [("py.md", "trigger")]
    assert data["text"].startswith("Память (рефлекс по ошибке)")
    assert 'read_article("testproj", "py.md")' in data["text"]


def test_reflex_endpoint_returns_only_rendered_memos(knowledge_dir, monkeypatch):
    """Не влезшая в бюджет памятка не отдаётся: иначе хук пометил бы её показанной."""
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    for i in range(3):
        (knowledge_dir / "testproj" / f"long{i}.md").write_text(
            f"# Статья {i} " + "очень длинный заголовок " * 40 + "\n\n"
            f"**Дата:** 2026-01-0{i + 1} 10:00\n\n## Записи\n\n### 2026-01-01 10:00\n" + "x" * 300
            + "\n\n## Рефлексы\n- цель: nas-long\n", encoding="utf-8")
    code, data = _call(JsonRequest({"kind": "target", "text": ["nas-long"]}))
    assert code == 200
    shown = [m["file"] for m in data["memos"]]
    assert 0 < len(shown) < 3, shown
    for f in shown:
        assert f'"{f}")' in data["text"]


def test_reflex_endpoint_rejects_bad_input():
    assert _call(JsonRequest(raw="{не json"))[0] == 400
    assert _call(JsonRequest({"kind": "что-то", "text": "x"}))[0] == 400
    assert _call(JsonRequest(["kind", "error"]))[0] == 400


def test_reflex_endpoint_empty_when_nothing_found(knowledge_dir, monkeypatch):
    monkeypatch.setattr(reflexes, "REFLEX_RESCAN_SEC", 0)
    reflexes.invalidate()
    code, data = _call(JsonRequest({"kind": "file", "text": "C:/nowhere/x.py",
                                    "exclude": "не список"}))
    assert code == 200 and data == {"memos": [], "text": ""}
