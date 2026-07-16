"""Tests for MCP resources (P1): статьи как memory://<проект>/<файл>.

Ресурсы — пассивный контекст (клиент листает/прикрепляет/@-упоминает). Секреты
(secret_*.md и инлайн ENC:) НЕ отдаются: файлы-секреты исключаются из листинга и
read_resource возвращает заглушку; в обычных статьях ENC:-фрагменты редактируются
(не расшифровываются).
"""
import asyncio

from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import Resource, ResourceTemplate

from memory_compiler.tools import list_resources, read_resource, list_resource_templates


def test_list_resources_exposes_articles(knowledge_dir):
    res = asyncio.run(list_resources())
    assert all(isinstance(r, Resource) for r in res)
    uris = [str(r.uri) for r in res]
    assert any("testproj/test_article.md" in u for u in uris)
    assert all(u.startswith("memory://") for u in uris)


def test_list_resources_excludes_secrets(knowledge_dir):
    (knowledge_dir / "testproj" / "secret_pw.md").write_text(
        "# secret\n\nENC:gAAAAAsecretcipher\n", encoding="utf-8")
    res = asyncio.run(list_resources())
    uris = [str(r.uri) for r in res]
    assert not any("secret_pw" in u for u in uris), "секрет не должен светиться в списке ресурсов"


def test_read_resource_returns_markdown(knowledge_dir):
    out = list(asyncio.run(read_resource("memory://testproj/test_article.md")))
    assert out and isinstance(out[0], ReadResourceContents)
    assert "docker deployment" in out[0].content
    assert out[0].mime_type == "text/markdown"


def test_read_resource_refuses_secret_file(knowledge_dir):
    (knowledge_dir / "testproj" / "secret_pw.md").write_text(
        "# secret\n\nsuper-secret-value-42\n", encoding="utf-8")
    out = list(asyncio.run(read_resource("memory://testproj/secret_pw.md")))
    txt = out[0].content
    assert "super-secret-value-42" not in txt, "секрет не должен утечь через ресурс"
    assert "секрет" in txt.lower() or "недоступ" in txt.lower()


def test_read_resource_redacts_enc_lines(knowledge_dir):
    (knowledge_dir / "testproj" / "mixed.md").write_text(
        "# Mixed\n\n## Записи\n\nENC:gAAAAAsecretcipherblob\nобычная строка\n", encoding="utf-8")
    out = list(asyncio.run(read_resource("memory://testproj/mixed.md")))
    txt = out[0].content
    assert "gAAAAAsecretcipherblob" not in txt, "ENC-фрагмент не должен отдаваться сырым"
    assert "обычная строка" in txt


def test_read_resource_unknown_returns_notice(knowledge_dir):
    out = list(asyncio.run(read_resource("memory://testproj/nope.md")))
    assert out and ("не найден" in out[0].content.lower() or "not found" in out[0].content.lower())


def test_read_resource_percent_encoded_cyrillic(knowledge_dir):
    """URI кириллических статей приходит percent-энкодированным (AnyUrl) — read_resource
    должен раскодировать путь, иначе файл «не найден». Регресс за живым багом v1.12.0."""
    from urllib.parse import quote
    (knowledge_dir / "testproj" / "тест_статья.md").write_text(
        "# Тест\n\nсодержимое статьи\n", encoding="utf-8")
    enc = "memory://testproj/" + quote("тест_статья.md")
    out = list(asyncio.run(read_resource(enc)))
    assert "содержимое статьи" in out[0].content, "percent-encoded кириллица должна раскодироваться"


def test_list_resource_templates(knowledge_dir):
    tpls = asyncio.run(list_resource_templates())
    assert all(isinstance(t, ResourceTemplate) for t in tpls)
    assert any("memory://" in t.uriTemplate for t in tpls)
