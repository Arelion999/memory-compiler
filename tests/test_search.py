"""Tests for search module."""
from memory_compiler.search import rebuild_index, _parse_article, is_low_confidence_query, _content_tokens


def test_parse_article():
    text = "# My Title\n\n**Теги:** docker, test\n\nBody text here"
    result = _parse_article(text, "my_title.md", "testproj")
    assert result["title"] == "My Title"
    assert "docker" in result["tags"]
    assert result["project"] == "testproj"
    assert result["path"] == "testproj/my_title.md"


def test_rebuild_index(knowledge_dir):
    count = rebuild_index()
    assert count >= 1


def test_low_confidence_query_continuation():
    # Generic continuation phrases — should be flagged as low confidence
    assert is_low_confidence_query("давай продолжим")
    assert is_low_confidence_query("продолжаем работу")
    assert is_low_confidence_query("давай дальше")
    assert is_low_confidence_query("let's continue")
    assert is_low_confidence_query("what's next")
    assert is_low_confidence_query("ok")
    assert is_low_confidence_query("")
    assert is_low_confidence_query("да")


def test_low_confidence_query_specific_pass():
    # Specific topic queries — must NOT be flagged
    assert not is_low_confidence_query("nginx ssl prod config")
    assert not is_low_confidence_query("POST /v1/orders endpoint")
    assert not is_low_confidence_query("deploy backend service")
    assert not is_low_confidence_query("ConnectionRefused error 5432 postgres")


def test_low_confidence_query_mixed():
    # Mixed — has at least 2 content tokens → not low confidence
    assert not is_low_confidence_query("давай настроим nginx mikrotik")  # nginx + mikrotik
    # Only one content token → still low confidence
    assert is_low_confidence_query("давай продолжим nginx")  # only "nginx"


def test_content_tokens_strips_stopwords():
    tokens = _content_tokens("давай продолжим работу по nginx и mikrotik")
    assert "nginx" in tokens
    assert "mikrotik" in tokens
    assert "давай" not in tokens
    assert "работу" not in tokens
    assert "продолжим" not in tokens
