"""Параметр triggers у save_lesson / finish_task / edit_article (v1.78.0)."""
import asyncio

import memory_compiler.config as cfg
import memory_compiler.handlers as handlers_mod
from memory_compiler import reflexes
from memory_compiler.handlers import edit_article, finish_task, save_lesson
from memory_compiler.tools import list_tools


def _read(kd, project, pattern):
    return next((kd / project).glob(pattern)).read_text(encoding="utf-8")


def test_save_lesson_writes_triggers_and_reports(knowledge_dir):
    res = asyncio.run(save_lesson("SFTP на NAS выключен", "Файлы через tar.", "testproj",
                                  triggers=["ошибка: Unable to start subsystem: sftp", "цель: x"]))
    text = _read(knowledge_dir, "testproj", "sftp*.md")
    assert reflexes.parse_triggers(text) == [("error", "Unable to start subsystem: sftp")]
    assert "🧷 Рефлексы: +1 (ошибка)" in res[0].text
    assert "не принят" in res[0].text


def test_finish_task_passes_triggers(knowledge_dir):
    asyncio.run(finish_task("Python не находится", "Звать python, а не python3.", "testproj",
                            triggers=["ошибка: Python was not found; run without arguments"]))
    text = _read(knowledge_dir, "testproj", "python*.md")
    assert reflexes.parse_triggers(text)[0][0] == "error"


def test_edit_article_triggers_only_keeps_body_and_dependents(knowledge_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(handlers_mod, "mark_dependents", lambda *a, **k: calls.append(a) or 0)
    path = knowledge_dir / "testproj" / "test_article.md"
    before = path.read_text(encoding="utf-8")
    res = asyncio.run(edit_article("testproj", "test_article.md",
                                   triggers=["файл: memory-compiler/docker-compose.yml"]))
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before.rstrip("\n"))
    assert reflexes.parse_triggers(after) == [("file", "memory-compiler/docker-compose.yml")]
    assert "Рефлексы" in res[0].text
    assert calls == [], "содержимое статьи не менялось — зависимым нечего помечать"


def test_edit_article_triggers_on_secret_need_no_key(knowledge_dir, monkeypatch):
    """Одни триггеры в секрет — без шифрования и без MC_ENCRYPT_KEY; тело не трогается."""
    monkeypatch.setattr(cfg, "MC_ENCRYPT_KEY", "")
    path = knowledge_dir / "testproj" / "secret_nas.md"
    path.write_text("# Доступы NAS\n\n**Дата:** 2026-01-01 10:00\n**Секрет:** да\n\nENC:abcdef\n",
                    encoding="utf-8")
    res = asyncio.run(edit_article("testproj", "secret_nas.md", triggers=["цель: nas-main"]))
    text = path.read_text(encoding="utf-8")
    assert "❌" not in res[0].text
    assert "ENC:abcdef" in text
    assert reflexes.parse_triggers(text) == [("target", "nas-main")]


def test_edit_article_without_content_and_triggers_is_refused(knowledge_dir):
    path = knowledge_dir / "testproj" / "test_article.md"
    before = path.read_text(encoding="utf-8")
    res = asyncio.run(edit_article("testproj", "test_article.md"))
    assert "Нечего менять" in res[0].text
    assert path.read_text(encoding="utf-8") == before


def test_schemas_declare_triggers_and_edit_content_optional():
    tools_by_name = {t.name: t for t in asyncio.run(list_tools())}
    for name in ("save_lesson", "finish_task", "edit_article"):
        spec = tools_by_name[name].inputSchema["properties"]["triggers"]
        assert spec["type"] == "array" and spec["items"] == {"type": "string"}
        assert "ошибка:" in spec["description"]
    assert "content" not in tools_by_name["edit_article"].inputSchema["required"]
