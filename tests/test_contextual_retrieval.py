from memory_compiler.search import _split_body, CHUNK_BODY_MAX


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
