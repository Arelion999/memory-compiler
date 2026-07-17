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
