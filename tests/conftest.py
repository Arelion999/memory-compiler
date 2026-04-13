"""Shared test fixtures."""
import pytest
from pathlib import Path


@pytest.fixture
def knowledge_dir(tmp_path):
    """Create a temporary knowledge directory with test data."""
    kd = tmp_path / "knowledge"
    kd.mkdir()
    proj = kd / "testproj"
    proj.mkdir()
    article = proj / "test_article.md"
    article.write_text(
        "# Test Article\n\n"
        "**Дата:** 2026-01-01 10:00\n"
        "**Проект:** testproj\n"
        "**Теги:** docker, test\n\n"
        "## Записи\n\n"
        "### 2026-01-01 10:00\n"
        "Test content about docker deployment on NAS.\n",
        encoding="utf-8",
    )
    daily = kd / "daily"
    daily.mkdir()
    general = kd / "general"
    general.mkdir()
    return kd


@pytest.fixture(autouse=True)
def patch_knowledge_dir(knowledge_dir, monkeypatch):
    """Patch KNOWLEDGE_DIR for all tests across all modules that import it."""
    import memory_compiler.config as cfg
    import memory_compiler.storage as storage_mod
    import memory_compiler.search as search_mod
    import memory_compiler.handlers as handlers_mod

    # Patch config module (canonical source)
    monkeypatch.setattr(cfg, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(cfg, "INDEX_DIR", knowledge_dir / ".whoosh_index")
    monkeypatch.setattr(cfg, "ARTICLE_META_PATH", knowledge_dir / ".article_meta.json")
    monkeypatch.setattr(cfg, "article_meta", {})
    monkeypatch.setattr(cfg, "PROJECTS", ["testproj", "general"])

    # Patch local bindings in modules that use `from config import KNOWLEDGE_DIR`
    monkeypatch.setattr(storage_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(search_mod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(search_mod, "INDEX_DIR", knowledge_dir / ".whoosh_index")
    monkeypatch.setattr(search_mod, "EMBEDDINGS_PATH", knowledge_dir / ".embeddings.pkl")
    monkeypatch.setattr(handlers_mod, "KNOWLEDGE_DIR", knowledge_dir)

    # Reset whoosh index so it gets recreated in tmp dir
    monkeypatch.setattr(search_mod, "_ix", None)
