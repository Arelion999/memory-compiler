from memory_compiler.search import _split_body, CHUNK_BODY_MAX
from memory_compiler.search import _section_context, _article_contexts


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
