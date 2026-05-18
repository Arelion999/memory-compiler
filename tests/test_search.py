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
    # One specific content token is still actionable — must NOT be low confidence
    # (Web UI single-word search like "memory-compiler" or "nginx" must work)
    assert not is_low_confidence_query("давай продолжим nginx")  # "nginx" is enough


def test_low_confidence_single_word_passes():
    """Single-word non-stopword queries (e.g. from Web UI search bar) must NOT
    be flagged as low-confidence. Previously they were filtered out entirely."""
    assert not is_low_confidence_query("memory-compiler")
    assert not is_low_confidence_query("nginx")
    assert not is_low_confidence_query("postgres")
    assert not is_low_confidence_query("mireks_ut")


def test_content_tokens_strips_stopwords():
    tokens = _content_tokens("давай продолжим работу по nginx и mikrotik")
    assert "nginx" in tokens
    assert "mikrotik" in tokens
    assert "давай" not in tokens
    assert "работу" not in tokens
    assert "продолжим" not in tokens


def test_embed_document_uses_chunking_strategy(knowledge_dir, monkeypatch):
    """embed_document must use the same chunking strategy as rebuild_embeddings.
    With LATE_CHUNKING=true it should embed the whole document — same shape that
    rebuild_embeddings produces — so newly-saved articles aren't second-class."""
    monkeypatch.setenv("LATE_CHUNKING", "true")
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    import memory_compiler.search as _smod
    monkeypatch.setattr(_smod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(_smod, "INDEX_DIR", knowledge_dir / ".whoosh_index")
    monkeypatch.setattr(_smod, "EMBEDDINGS_PATH", knowledge_dir / ".embeddings.pkl")
    monkeypatch.setattr(_smod, "_ix", None)
    _smod._embeddings.clear()
    _smod._embed_texts.clear()

    proj = knowledge_dir / "testproj"
    text = (
        "# New article\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n### Section A\nfirst section body\n\n### Section B\nsecond section body"
    )
    (proj / "new.md").write_text(text, encoding="utf-8")

    _smod.embed_document(text, "new.md", "testproj")
    keys = [k for k in _smod._embeddings if "new.md" in k]
    chunk_keys = [k for k in keys if "#chunk" in k]
    # Late chunking → exactly 1 key for the article (no #chunkN suffix)
    assert len(chunk_keys) == 0, f"late chunking should not split, got chunks: {chunk_keys}"
    assert len(keys) == 1, f"should be 1 key per article, got: {keys}"

    monkeypatch.delenv("LATE_CHUNKING", raising=False)
    importlib.reload(memory_compiler.search)


def test_rebuild_embeddings_atomic_on_failure(knowledge_dir, monkeypatch):
    """If model.encode fails mid-rebuild, the previous _embeddings dict must
    remain intact — semantic search must keep working with old data."""
    import memory_compiler.search as _smod
    import memory_compiler.config as _cfg

    proj = knowledge_dir / "testproj"
    (proj / "first.md").write_text(
        "# First\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\nFirst article body content.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    # 1. Successful initial rebuild — populates _embeddings
    _smod.rebuild_embeddings()
    initial = dict(_smod._embeddings)
    assert len(initial) >= 1, "initial rebuild should populate embeddings"

    # 2. Make rebuild fail mid-flight by monkeypatching model.encode
    class BoomModel:
        def encode(self, *a, **kw):
            raise RuntimeError("synthetic OOM")
    monkeypatch.setattr(_smod, "_embed_model", BoomModel())

    # 3. Add another article and trigger rebuild — must fail but NOT wipe state
    (proj / "second.md").write_text(
        "# Second\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\nSecond body content.",
        encoding="utf-8",
    )
    try:
        _smod.rebuild_embeddings()
    except RuntimeError:
        pass  # expected
    # 4. Previous embeddings must be preserved (atomic semantics)
    assert _smod._embeddings == initial, \
        "rebuild_embeddings must not wipe state on failure"


def test_embed_batch_size_default_safe():
    """Default EMBED_BATCH_SIZE must be small enough to avoid OOM on NAS-class hosts."""
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    from memory_compiler.search import EMBED_BATCH_SIZE, EMBED_MAX_SEQ_LENGTH
    # 8 keeps peak allocation ~270MB even with seq=2048 hidden=1024
    assert EMBED_BATCH_SIZE <= 16
    assert EMBED_MAX_SEQ_LENGTH <= 4096


def test_embed_batch_size_env_override(monkeypatch):
    """EMBED_BATCH_SIZE and EMBED_MAX_SEQ_LENGTH must be configurable via env."""
    monkeypatch.setenv("EMBED_BATCH_SIZE", "32")
    monkeypatch.setenv("EMBED_MAX_SEQ_LENGTH", "512")
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    from memory_compiler.search import EMBED_BATCH_SIZE, EMBED_MAX_SEQ_LENGTH
    assert EMBED_BATCH_SIZE == 32
    assert EMBED_MAX_SEQ_LENGTH == 512
    monkeypatch.delenv("EMBED_BATCH_SIZE", raising=False)
    monkeypatch.delenv("EMBED_MAX_SEQ_LENGTH", raising=False)
    importlib.reload(memory_compiler.search)


def test_splade_disabled_by_default():
    """SPLADE 3-way hybrid is opt-in — default must be disabled."""
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    from memory_compiler.search import SPLADE_ENABLED
    assert SPLADE_ENABLED is False


def test_splade_env_enables(monkeypatch):
    """SPLADE_ENABLED=true must flip the flag."""
    monkeypatch.setenv("SPLADE_ENABLED", "true")
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    from memory_compiler.search import SPLADE_ENABLED
    assert SPLADE_ENABLED is True
    monkeypatch.delenv("SPLADE_ENABLED", raising=False)
    importlib.reload(memory_compiler.search)


def test_search_works_when_splade_enabled_but_model_missing(knowledge_dir, monkeypatch):
    """Even with SPLADE_ENABLED=true, search must gracefully fall back if model unavailable."""
    monkeypatch.setenv("SPLADE_ENABLED", "true")
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    import memory_compiler.search as _smod
    import memory_compiler.config as _cfg
    # Re-patch knowledge_dir after reload
    monkeypatch.setattr(_smod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(_smod, "INDEX_DIR", knowledge_dir / ".whoosh_index")
    monkeypatch.setattr(_smod, "EMBEDDINGS_PATH", knowledge_dir / ".embeddings.pkl")
    monkeypatch.setattr(_smod, "_ix", None)

    proj = knowledge_dir / "testproj"
    (proj / "thing.md").write_text(
        "# Postgres backup\n\n**Дата:** 2026-01-01 10:00\n**Теги:** postgres\n\n"
        "## Записи\n\n### 2026-01-01 10:00\nBackup procedure for postgres body line.",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    _smod.rebuild_index()
    _smod.rebuild_embeddings()

    # Search must succeed even though SPLADE model not loaded
    results = _smod.whoosh_search("postgres backup", project="testproj", limit=5)
    assert results, "Search must return results even when SPLADE model is unavailable"

    monkeypatch.delenv("SPLADE_ENABLED", raising=False)
    importlib.reload(memory_compiler.search)


def test_late_chunking_disabled_by_default_produces_chunks(knowledge_dir):
    """Default (LATE_CHUNKING=false): article with ### sections produces multiple chunk embeddings."""
    import memory_compiler.config as _cfg
    import memory_compiler.search as _smod

    proj = knowledge_dir / "testproj"
    (proj / "multi_section.md").write_text(
        "# Big article\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n"
        "### Section A\nContent of section A about nginx setup details.\n\n"
        "### Section B\nContent of section B about postgres tuning details.\n\n"
        "### Section C\nContent of section C about redis cache details.\n",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    _smod.rebuild_embeddings()

    keys = [k for k in _smod._embeddings.keys() if "multi_section.md" in k]
    # Default chunked mode: should have >1 keys for an article with 3 ### sections
    chunk_keys = [k for k in keys if "#chunk" in k]
    assert len(chunk_keys) >= 2, f"Default chunking should produce chunk keys, got: {keys}"


def test_late_chunking_enabled_produces_single_embedding(knowledge_dir, monkeypatch):
    """LATE_CHUNKING=true: article gets ONE whole-document embedding instead of N chunks."""
    monkeypatch.setenv("LATE_CHUNKING", "true")
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    import memory_compiler.config as _cfg
    import memory_compiler.search as _smod
    # Re-patch the just-reloaded module to use knowledge_dir
    monkeypatch.setattr(_smod, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(_smod, "INDEX_DIR", knowledge_dir / ".whoosh_index")
    monkeypatch.setattr(_smod, "EMBEDDINGS_PATH", knowledge_dir / ".embeddings.pkl")
    monkeypatch.setattr(_smod, "_ix", None)

    proj = knowledge_dir / "testproj"
    (proj / "multi_section2.md").write_text(
        "# Big article 2\n\n**Дата:** 2026-01-01 10:00\n**Теги:** topic\n\n"
        "## Записи\n\n"
        "### Section A\nContent of section A about nginx setup details.\n\n"
        "### Section B\nContent of section B about postgres tuning details.\n\n",
        encoding="utf-8",
    )
    _cfg.PROJECTS = _cfg._discover_projects()
    _smod.rebuild_embeddings()

    keys = [k for k in _smod._embeddings.keys() if "multi_section2.md" in k]
    chunk_keys = [k for k in keys if "#chunk" in k]
    # Late chunking: NO chunk keys, just one whole-doc embedding
    assert len(chunk_keys) == 0, f"Late chunking should NOT split into chunks, got chunks: {chunk_keys}"
    assert len(keys) == 1, f"Late chunking should produce exactly 1 embedding per article, got: {keys}"

    # Cleanup: reload module to restore default
    monkeypatch.delenv("LATE_CHUNKING", raising=False)
    importlib.reload(memory_compiler.search)


def test_embed_model_env_override(monkeypatch):
    """EMBED_MODEL env var must override the default embedding model."""
    monkeypatch.setenv("EMBED_MODEL", "BAAI/bge-m3")
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    from memory_compiler.search import EMBED_MODEL_NAME
    assert EMBED_MODEL_NAME == "BAAI/bge-m3"
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    importlib.reload(memory_compiler.search)


def test_embeddings_pkl_stores_model_name(knowledge_dir):
    """Saved .embeddings.pkl must include the model_name for cache invalidation."""
    import pickle
    from memory_compiler.search import rebuild_embeddings, EMBEDDINGS_PATH
    rebuild_embeddings()
    assert EMBEDDINGS_PATH.exists()
    with open(EMBEDDINGS_PATH, "rb") as f:
        data = pickle.load(f)
    assert "model" in data, f"pkl must store 'model' field; got keys: {list(data.keys())}"
    assert data["model"], "model field must be non-empty"


def test_load_embeddings_invalidates_on_late_chunking_mismatch(knowledge_dir, monkeypatch):
    """If pkl was saved with one LATE_CHUNKING value, loading with the opposite
    must invalidate the cache — embedding topology differs (whole-doc vs chunks)."""
    import pickle
    from memory_compiler.search import EMBEDDINGS_PATH, load_embeddings, EMBED_MODEL_NAME
    import memory_compiler.search as _smod
    # Save pkl as if it were produced with LATE_CHUNKING=True
    fake_pkl = {
        "model": EMBED_MODEL_NAME,
        "late_chunking": True,
        "embeddings": {"foo/bar.md": [0.1, 0.2]},
        "texts": {"foo/bar.md": "bar"},
    }
    EMBEDDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EMBEDDINGS_PATH, "wb") as f:
        pickle.dump(fake_pkl, f)
    # Current runtime has LATE_CHUNKING=False (default in module) — mismatch → invalidate
    monkeypatch.setattr(_smod, "LATE_CHUNKING", False)
    assert load_embeddings() is False, "different LATE_CHUNKING must invalidate cache"


def test_load_embeddings_invalidates_on_model_mismatch(knowledge_dir):
    """If pkl was saved by a different model, load_embeddings must refuse to use it."""
    import pickle
    from memory_compiler.search import EMBEDDINGS_PATH, load_embeddings
    # Write a pkl with mismatched model name
    fake_pkl = {
        "model": "totally-different-model-name",
        "embeddings": {},
        "texts": {},
    }
    EMBEDDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EMBEDDINGS_PATH, "wb") as f:
        pickle.dump(fake_pkl, f)
    assert load_embeddings() is False, "Cache from another model must be invalidated"


def test_reranker_default_is_multilingual_v2():
    """Default reranker must be a multilingual model (bge-reranker-v2-m3 by default).
    Russian-heavy KB benefits from multilingual cross-encoder."""
    from memory_compiler.search import RERANKER_MODEL_NAME
    # v2-m3 = multilingual (BGE-M3 base), large quality jump over -base for RU
    assert "v2" in RERANKER_MODEL_NAME or "m3" in RERANKER_MODEL_NAME, \
        f"Default reranker should be multilingual v2/m3, got: {RERANKER_MODEL_NAME}"


def test_reranker_model_env_override(monkeypatch):
    """RERANKER_MODEL env var must override the default model name."""
    monkeypatch.setenv("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    # Re-import to pick up env var
    import importlib
    import memory_compiler.search
    importlib.reload(memory_compiler.search)
    from memory_compiler.search import RERANKER_MODEL_NAME
    assert RERANKER_MODEL_NAME == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # Restore default (other tests rely on it)
    monkeypatch.delenv("RERANKER_MODEL", raising=False)
    importlib.reload(memory_compiler.search)


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
