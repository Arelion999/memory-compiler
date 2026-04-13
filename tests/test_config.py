"""Tests for config module."""
import memory_compiler.config as cfg
from memory_compiler.config import decay_factor, track_access


def test_decay_factor_unknown_path():
    assert decay_factor("nonexistent/file.md") == 0.7


def test_track_access(knowledge_dir):
    track_access(["testproj/test_article.md"])
    assert "testproj/test_article.md" in cfg.article_meta
    assert cfg.article_meta["testproj/test_article.md"]["access_count"] == 1
    track_access(["testproj/test_article.md"])
    assert cfg.article_meta["testproj/test_article.md"]["access_count"] == 2
