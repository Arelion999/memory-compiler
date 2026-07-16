"""Tests for MCP completion (P2): автодополнение аргументов промптов/ресурсов.

Клиент (Claude Desktop) при вводе аргумента project (в слэш-командах и в шаблоне
ресурса memory://{project}/{filename}) получает валидные имена проектов по мере ввода;
filename — статьи выбранного проекта.
"""
import asyncio

from mcp.types import (
    Completion, CompletionArgument, CompletionContext,
    PromptReference, ResourceTemplateReference,
)

from memory_compiler.tools import complete


def _run(ref, arg_name, value, ctx_args=None):
    arg = CompletionArgument(name=arg_name, value=value)
    ctx = CompletionContext(arguments=ctx_args) if ctx_args is not None else None
    return asyncio.run(complete(ref, arg, ctx))


def test_project_completion_for_prompt(knowledge_dir):
    ref = PromptReference(type="ref/prompt", name="load-context")
    res = _run(ref, "project", "test")
    assert isinstance(res, Completion)
    assert "testproj" in res.values
    assert "daily" not in res.values  # daily — не проект


def test_project_completion_empty_value_lists_all(knowledge_dir):
    ref = PromptReference(type="ref/prompt", name="save-session")
    res = _run(ref, "project", "")
    assert "testproj" in res.values and "general" in res.values


def test_project_completion_prefix_filter(knowledge_dir):
    (knowledge_dir / "infra").mkdir(exist_ok=True)
    ref = PromptReference(type="ref/prompt", name="load-context")
    res = _run(ref, "project", "inf")
    assert res.values == ["infra"]


def test_filename_completion_for_resource_template(knowledge_dir):
    ref = ResourceTemplateReference(type="ref/resource", uri="memory://{project}/{filename}")
    res = _run(ref, "filename", "", ctx_args={"project": "testproj"})
    assert "test_article.md" in res.values


def test_filename_completion_excludes_secrets(knowledge_dir):
    (knowledge_dir / "testproj" / "secret_pw.md").write_text("# s\n", encoding="utf-8")
    ref = ResourceTemplateReference(type="ref/resource", uri="memory://{project}/{filename}")
    res = _run(ref, "filename", "", ctx_args={"project": "testproj"})
    assert not any("secret_pw" in v for v in res.values)


def test_unknown_argument_returns_empty(knowledge_dir):
    ref = PromptReference(type="ref/prompt", name="save-lesson")
    res = _run(ref, "topic", "ngi")
    assert isinstance(res, Completion)
    assert res.values == []
