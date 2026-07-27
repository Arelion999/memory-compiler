"""Guard от утёкшей разметки вызова (клиентский парсер не закрывает параметр:
хвост '</param>' + чужие поля + '</invoke>' въезжает в строковое значение).
Замер 2026-07-27: 208 живых статей с паттерном, у ~50 сессий потеряны
session_summary/open_questions, tags ставил только авто-теггер.
Лечение — на транспортной границе (call_tool), словарь тегов — имена параметров
самого тула; якорь ТОЛЬКО на конце строки: упоминание тегов в середине текста
(статьи про сам баг) не трогается."""
import asyncio
import json

import memory_compiler.tools as t
import memory_compiler.search as s


def _props(tool_name: str) -> dict:
    tools = asyncio.run(t.list_tools())
    tool = next(x for x in tools if x.name == tool_name)
    return tool.inputSchema.get("properties", {})


# ─── unit: heal_arguments ────────────────────────────────────────────────────


def test_heal_bare_anchor_stripped():
    args, healed = t.heal_arguments({"content": "текст урока.</content>"},
                                    _props("save_lesson"))
    assert args["content"] == "текст урока."
    assert "content" in healed


def test_heal_full_tail_recovers_fields_variant_a():
    content = ("суть диагноза.</content>\n"
               "<session_summary>СЕССИЯ_МАРКЕР итог сессии</session_summary>\n"
               "<open_questions>ВОПРОС_МАРКЕР открыт</open_questions>\n"
               '<tags>["тег1", "тег2"]</tags>\n'
               "</invoke>")
    args, healed = t.heal_arguments(
        {"topic": "T", "content": content, "project": "testproj"},
        _props("finish_task"))
    assert args["content"] == "суть диагноза."
    assert args["session_summary"] == "СЕССИЯ_МАРКЕР итог сессии"
    assert args["open_questions"] == "ВОПРОС_МАРКЕР открыт"
    assert args["tags"] == ["тег1", "тег2"]
    assert "content" in healed


def test_heal_parameter_syntax_variant_b():
    content = ("суть урока.</content>\n"
               '<parameter name="tags">["тег3"]</parameter>\n'
               "</invoke>")
    args, _ = t.heal_arguments({"topic": "T", "content": content, "project": "p"},
                               _props("save_lesson"))
    assert args["content"] == "суть урока."
    assert args["tags"] == ["тег3"]


def test_heal_mid_text_mentions_untouched():
    content = ("мусор вида «…текст.</content> + <session_summary>…</session_summary>» "
               "поражает 208 статей; лечится guard'ом.")
    args, healed = t.heal_arguments({"content": content}, _props("finish_task"))
    assert args["content"] == content
    assert healed == []


def test_heal_trailing_block_without_anchor_rolled_back():
    content = 'текст со вставкой <tags>["x"]</tags>'
    args, healed = t.heal_arguments({"content": content}, _props("finish_task"))
    assert args["content"] == content
    assert healed == []


def test_heal_explicit_arg_not_overridden():
    content = ("суть.</content>\n"
               "<session_summary>из хвоста</session_summary>\n</invoke>")
    args, _ = t.heal_arguments(
        {"content": content, "session_summary": "явный аргумент"},
        _props("finish_task"))
    assert args["content"] == "суть."
    assert args["session_summary"] == "явный аргумент"


def test_heal_bare_trailing_invoke_stripped():
    args, healed = t.heal_arguments({"content": "текст.\n</invoke>"},
                                    _props("save_lesson"))
    assert args["content"] == "текст."
    assert "content" in healed


def test_heal_broken_tags_json_dropped_not_crashing():
    content = ("суть.</content>\n<tags>[не json</tags>\n</invoke>")
    args, _ = t.heal_arguments({"content": content}, _props("save_lesson"))
    assert args["content"] == "суть."
    assert "tags" not in args


# ─── e2e: через диспетчер call_tool ──────────────────────────────────────────


def test_call_tool_finish_task_leak_healed_end_to_end(knowledge_dir, monkeypatch):
    import numpy as np
    monkeypatch.setattr(s, "encode_passages",
                        lambda texts, progress_label=None: [np.array([1.0, 0.0]) for _ in texts])
    content = ("ПРОБЛЕМА и решение задачи, содержательный текст.</content>\n"
               "<session_summary>СЕССИЯ_МАРКЕР_E2E итог</session_summary>\n"
               '<tags>["маркер_тег_e2e"]</tags>\n'
               "</invoke>")
    asyncio.run(t.call_tool("finish_task", {
        "topic": "leak guard e2e", "content": content, "project": "testproj"}))

    texts = {p: p.read_text(encoding="utf-8") for p in knowledge_dir.rglob("*.md")}
    art = next(txt for p, txt in texts.items()
               if "содержательный текст" in txt and p.parent.name == "testproj")
    assert "</content>" not in art and "<session_summary>" not in art \
        and "</invoke>" not in art
    # утёкшие поля доехали по назначению, а не выброшены
    assert any("СЕССИЯ_МАРКЕР_E2E" in txt for txt in texts.values())
    assert "маркер_тег_e2e" in art
    # аудит фиксирует срабатывание guard'а
    audit_last = (knowledge_dir / "_audit.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    entry = json.loads(audit_last)
    assert entry["tool"] == "finish_task" and entry["args"].get("_healed")


def test_call_tool_clean_call_untouched(knowledge_dir, monkeypatch):
    """Здоровый вызов guard не трогает и в аудит маркер не пишет."""
    import numpy as np
    monkeypatch.setattr(s, "encode_passages",
                        lambda texts, progress_label=None: [np.array([1.0, 0.0]) for _ in texts])
    asyncio.run(t.call_tool("save_lesson", {
        "topic": "чистый вызов", "content": "обычный текст без хвостов",
        "project": "testproj"}))
    audit_last = (knowledge_dir / "_audit.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "_healed" not in json.loads(audit_last)["args"]
