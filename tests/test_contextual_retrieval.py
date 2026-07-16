from memory_compiler.search import _split_body, CHUNK_BODY_MAX
from memory_compiler.search import _section_context, _article_contexts
from memory_compiler.search import _chunk_article
import pickle
import memory_compiler.search as smod


def test_split_body_short_returns_single():
    assert _split_body("короткое тело") == ["короткое тело"]


def test_split_body_empty_returns_one_empty():
    assert _split_body("") == [""]


def test_split_body_long_splits_into_windows_within_limit():
    body = "\n".join(f"строка номер {i} с некоторым содержимым" for i in range(200))
    windows = _split_body(body, max_len=300)
    assert len(windows) > 1
    assert all(len(w) <= 300 for w in windows)
    assert any("строка номер 199" in w for w in windows)


def test_split_body_single_overlong_line_hard_split():
    line = "x" * 1000
    windows = _split_body(line, max_len=300)
    assert all(len(w) <= 300 for w in windows)
    assert "".join(windows) == line


def test_section_context_metadata_includes_project_title_section_tags():
    ctx = _section_context("infra", "Инфраструктура: серверы", "nginx,ssl", "nginx_niksdv", {})
    assert ctx.startswith("[") and ctx.endswith("]")
    for token in ("infra", "Инфраструктура: серверы", "nginx_niksdv", "nginx,ssl"):
        assert token in ctx


def test_section_context_frontmatter_overrides_metadata():
    ai = {"nginx_niksdv": "SSH-доступ и SSL сервера nginx_niksdv"}
    ctx = _section_context("infra", "Инфраструктура", "nginx", "nginx_niksdv", ai)
    assert ctx == "SSH-доступ и SSL сервера nginx_niksdv"


def test_article_contexts_reads_frontmatter():
    text = ("---\ncontexts:\n  \"sec A\": \"контекст A\"\n---\n"
            "# Заголовок\n\nтело\n")
    assert _article_contexts(text) == {"sec A": "контекст A"}


def test_article_contexts_empty_when_absent():
    assert _article_contexts("# Заголовок\n\nтело без frontmatter\n") == {}


def _multi_section_article():
    return (
        "# NiksDesk: nginx конфиг\n"
        "**Теги:** nginx, ssl\n\n"
        "## Записи\n\n"
        "### nginx_niksdv\n"
        "SSH: root, ключ MEGABOOK_S1\n\n"
        "### DNS\n"
        + ("подробное описание DNS-записей " * 60) + "\n"
    )


def test_chunk_article_includes_project_name():
    chunks = _chunk_article(_multi_section_article(), "niksdesk/max.md")
    joined = " ".join(t for _, t in chunks)
    assert "niksdesk" in joined


def test_chunk_article_no_body_truncation():
    chunks = _chunk_article(_multi_section_article(), "niksdesk/max.md")
    joined = " ".join(t for _, t in chunks)
    # Старый код резал секцию до body[:400] → сохранялось ~12 из 60 повторов фразы.
    # Новый код НЕ обрезает: длинная секция режется на под-чанки, каждый с контекст-
    # префиксом. Из-за инъекции ctx между окнами ~3 повтора фразы попадают на границу
    # окна и «рвутся» (символы на месте, но точная подстрока не матчится) — поэтому
    # порог 50, а не 60. 50 >> 12 однозначно доказывает отсутствие обрезки хвоста.
    assert joined.count("подробное описание DNS-записей") >= 50


def test_chunk_article_frontmatter_context_used():
    text = ("---\ncontexts:\n  \"nginx_niksdv\": \"ИИ-контекст про nginx_niksdv\"\n---\n"
            + _multi_section_article())
    chunks = _chunk_article(text, "niksdesk/max.md")
    joined = " ".join(t for _, t in chunks)
    assert "ИИ-контекст про nginx_niksdv" in joined


def test_chunk_article_keys_start_with_path_key():
    chunks = _chunk_article(_multi_section_article(), "niksdesk/max.md")
    assert chunks and all(k == "niksdesk/max.md" or k.startswith("niksdesk/max.md#")
                          for k, _ in chunks)


def test_context_format_version_mismatch_triggers_rebuild(knowledge_dir, monkeypatch):
    monkeypatch.setattr(smod, "EMBEDDINGS_PATH", knowledge_dir / ".embeddings.pkl")
    smod.EMBEDDINGS_PATH.write_bytes(pickle.dumps({
        "model": smod.EMBED_MODEL_NAME,
        "late_chunking": smod.LATE_CHUNKING,
        "context_format_version": 0,
        "embeddings": {}, "texts": {}, "chunk_hashes": {},
    }))
    assert smod.load_embeddings() is False


def test_context_format_version_match_loads(knowledge_dir, monkeypatch):
    monkeypatch.setattr(smod, "EMBEDDINGS_PATH", knowledge_dir / ".embeddings.pkl")
    smod.EMBEDDINGS_PATH.write_bytes(pickle.dumps({
        "model": smod.EMBED_MODEL_NAME,
        "late_chunking": smod.LATE_CHUNKING,
        "context_format_version": smod.CONTEXT_FORMAT_VERSION,
        "embeddings": {}, "texts": {}, "chunk_hashes": {},
    }))
    assert smod.load_embeddings() is True


def test_chunk_article_caps_subchunks_per_section():
    """Огромная секция (ingested PDF/URL) не должна взрывать число чанков — кап
    CHUNK_MAX_SUBCHUNKS на секцию. Без капа тело ~21000 символов дало бы ~35 окон."""
    from memory_compiler.search import _chunk_article, CHUNK_MAX_SUBCHUNKS
    huge = "# T\n**Теги:** x\n\n## Записи\n\n### Big\n" + ("данные " * 3000)
    chunks = _chunk_article(huge, "proj/big.md")
    assert len(chunks) <= 2 * CHUNK_MAX_SUBCHUNKS, f"кап не сработал: {len(chunks)} чанков"
