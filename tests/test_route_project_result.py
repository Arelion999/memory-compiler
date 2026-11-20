"""Рекомендация маршрутизатора доступна клиенту без разбора Markdown."""

import asyncio

from memory_compiler import config, handlers, tools


def test_route_project_returns_machine_readable_cwd_choice(knowledge_dir, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS", ["testproj", "general"])
    result = asyncio.run(tools.call_tool("route_project", {"cwd": "C:/DEV/testproj"}))
    assert isinstance(result, tuple)
    content, route = result
    assert route == {"project": "testproj", "score": 100, "source": "cwd"}
    assert 'project="testproj"' in content[0].text


def test_unknown_directory_returns_general_without_guessing_name(knowledge_dir, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS", ["testproj", "general"])
    content, route = asyncio.run(tools.call_tool("route_project", {"cwd": "C:/DEV/AbsentClient"}))
    assert route == {"project": "general", "score": 0, "source": "fallback"}
    assert not (knowledge_dir / "absentclient").exists()


def test_ambiguous_text_keeps_general_recommendation(knowledge_dir, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS", ["alpha-app", "beta-app", "general"])
    async def no_matches(*args, **kwargs):
        return []
    monkeypatch.setattr(handlers, "_whoosh_async", no_matches)
    content, route = asyncio.run(tools.call_tool("route_project", {"text": "alpha-app beta-app"}))
    assert route == {"project": "general", "score": 0, "source": "ambiguous"}
    assert "Неоднозначно" in content[0].text


def test_strong_text_choice_matches_recommendation(knowledge_dir, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS", ["testproj", "general"])
    async def no_matches(*args, **kwargs):
        return []
    monkeypatch.setattr(handlers, "_whoosh_async", no_matches)
    content, route = asyncio.run(tools.call_tool("route_project", {"text": "testproj"}))
    assert route == {"project": "testproj", "score": 80, "source": "text"}


def test_route_choice_does_not_leak_to_next_call(knowledge_dir, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS", ["testproj", "general"])
    async def run():
        first = await tools.call_tool("route_project", {"cwd": "C:/DEV/testproj"})
        second = await tools.call_tool("route_project", {})
        assert first[1]["project"] == "testproj"
        assert second[1] == {"project": "general", "score": 0, "source": "fallback"}
    asyncio.run(run())
