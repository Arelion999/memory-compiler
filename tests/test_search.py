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


def test_soft_fallback_returns_low_confidence_when_top_weak(knowledge_dir):
    """When top score is in [LOW_CONF, HIGH_CONF), return up to 3 results
    marked with confidence='low' if they share query tokens. Avoids silent emptiness."""
    from memory_compiler.search import whoosh_search, rebuild_index, rebuild_embeddings
    proj = knowledge_dir / "soft"
    proj.mkdir(exist_ok=True)
    # Article that loosely mentions the term — score will be modest
    (proj / "weak.md").write_text(
        "# Random observation\n\n**Теги:** misc\n\nMentioned redis once in passing.",
        encoding="utf-8",
    )
    rebuild_index()
    rebuild_embeddings()

    # Query has token "redis" appearing in haystack but score is weak
    results = whoosh_search("redis configuration tuning patterns", limit=5)
    # Either we get the weak match marked low, or empty if score < LOW_CONF.
    # Critical: should NOT return mismatched articles claiming high confidence.
    if results:
        for r in results:
            # Either explicitly low-confidence or actually relevant
            assert r.get("confidence") == "low" or r.get("score", 0) >= 35
