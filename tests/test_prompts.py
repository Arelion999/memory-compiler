"""Tests for MCP prompts (P1): нативные слэш-команды для Claude Desktop.

list_prompts/get_prompt → в клиенте появляются /mcp__memory-compiler__load-context,
save-session, save-lesson, weekly-review. Промпты возвращают шаблонные сообщения-
инструкции (часть memory-autopilot как нативные команды).
"""
import asyncio

import pytest
from mcp.types import Prompt, GetPromptResult

from memory_compiler.tools import list_prompts, get_prompt


def test_list_prompts_exposes_commands():
    prompts = asyncio.run(list_prompts())
    assert all(isinstance(p, Prompt) for p in prompts)
    names = {p.name for p in prompts}
    assert {"load-context", "save-session", "save-lesson", "weekly-review"} <= names
    # у каждого есть описание
    assert all(p.description for p in prompts)


def test_load_context_injects_project():
    res = asyncio.run(get_prompt("load-context", {"project": "infra"}))
    assert isinstance(res, GetPromptResult)
    assert res.messages, "промпт должен вернуть хотя бы одно сообщение"
    text = " ".join(m.content.text for m in res.messages)
    assert "infra" in text


def test_load_context_requires_project():
    prompts = {p.name: p for p in asyncio.run(list_prompts())}
    arg = {a.name: a for a in prompts["load-context"].arguments}["project"]
    assert arg.required is True


def test_save_lesson_optional_topic():
    with_topic = asyncio.run(get_prompt("save-lesson", {"project": "infra", "topic": "nginx"}))
    text = " ".join(m.content.text for m in with_topic.messages)
    assert "nginx" in text and "infra" in text
    # без topic не падает
    no_topic = asyncio.run(get_prompt("save-lesson", {"project": "infra"}))
    assert no_topic.messages


def test_weekly_review_project_optional():
    res = asyncio.run(get_prompt("weekly-review", {}))
    assert res.messages
    res2 = asyncio.run(get_prompt("weekly-review", {"project": "niksdesk"}))
    text2 = " ".join(m.content.text for m in res2.messages)
    assert "niksdesk" in text2


def test_unknown_prompt_raises():
    with pytest.raises(Exception):
        asyncio.run(get_prompt("does-not-exist", {}))
