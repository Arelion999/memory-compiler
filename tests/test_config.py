"""Tests for config module."""
import memory_compiler.config as cfg
from memory_compiler.config import decay_factor, track_access, _bilingual_stem


def test_decay_factor_unknown_path():
    assert decay_factor("nonexistent/file.md") == 0.7


def test_track_access(knowledge_dir):
    track_access(["testproj/test_article.md"])
    assert "testproj/test_article.md" in cfg.article_meta
    assert cfg.article_meta["testproj/test_article.md"]["access_count"] == 1
    track_access(["testproj/test_article.md"])
    assert cfg.article_meta["testproj/test_article.md"]["access_count"] == 2


def test_bilingual_stemmer_english():
    # Inflected English forms reduce to common stem
    assert _bilingual_stem("deploys") == _bilingual_stem("deploying") == _bilingual_stem("deploy")
    assert _bilingual_stem("containers") == _bilingual_stem("container")


def test_bilingual_stemmer_russian():
    # Inflected Russian forms reduce to common stem
    base = _bilingual_stem("настройка")
    assert _bilingual_stem("настройки") == base
    assert _bilingual_stem("настройку") == base


def test_bilingual_stemmer_handles_empty_and_safe():
    assert _bilingual_stem("") == ""
    # Mixed-script word — shouldn't crash
    assert _bilingual_stem("nginx123") is not None
