"""Юнит-тесты чистого резолвера версий (memory_compiler/versioning.py)."""


def test_version_key_orders_by_numeric_components():
    from memory_compiler.versioning import version_key
    assert version_key("1.7.16") > version_key("1.7.9")
    assert version_key("1.7.16") > version_key("1.7.11")
    assert version_key("2.0.0") > version_key("1.9.9")


def test_version_key_prerelease_below_release():
    from memory_compiler.versioning import version_key
    assert version_key("1.8.0-rc1") < version_key("1.8.0")
    assert version_key("1.8.0-rc1") < version_key("1.8.0-rc2") < version_key("1.8.0")


def test_version_key_handles_four_part():
    from memory_compiler.versioning import version_key
    assert version_key("8.3.24.1234") > version_key("8.3.24.999")
    assert version_key("8.3.24.1234") > version_key("8.3.23.9999")


def test_is_date_like():
    from memory_compiler.versioning import is_date_like
    assert is_date_like("2024.06.25") is True
    assert is_date_like("2099.12.31") is True
    assert is_date_like("1.7.16") is False
    assert is_date_like("8.3.24") is False
    assert is_date_like("1999.06.25") is False
    assert is_date_like("2024.13.01") is False


def test_is_version_like():
    from memory_compiler.versioning import is_version_like
    assert is_version_like("1.8.0") is True
    assert is_version_like("1.2") is True
    assert is_version_like("8.3.24.1234") is True
    assert is_version_like("2024.06.25") is False
    assert is_version_like("hello") is False
    assert is_version_like("5") is False


def test_max_version_picks_highest():
    from memory_compiler.versioning import max_version
    assert max_version(["1.7.11", "1.7.16", "1.7.9"]) == "1.7.16"


def test_max_version_prefers_release_over_prerelease():
    from memory_compiler.versioning import max_version
    assert max_version(["1.8.0-rc1", "1.8.0"]) == "1.8.0"


def test_max_version_empty_returns_none():
    from memory_compiler.versioning import max_version
    assert max_version([]) is None


def test_max_version_single():
    from memory_compiler.versioning import max_version
    assert max_version(["1.2.3"]) == "1.2.3"
