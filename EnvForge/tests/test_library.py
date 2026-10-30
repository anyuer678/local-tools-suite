from __future__ import annotations

from envforge.library import CATEGORIES, all_presets, count, count_by_category, get, search, validate
from envforge.models import looks_sensitive_key


def test_catalog_covers_at_least_100_entries():
    assert count() >= 100
    assert len(all_presets()) == count()


def test_catalog_is_self_consistent():
    assert validate() == []


def test_catalog_keys_are_unique_and_valid():
    keys = [item.key for item in all_presets()]
    assert len(keys) == len({key.lower() for key in keys})
    for item in all_presets():
        assert item.key.replace("_", "").isalnum()
        assert not item.key[0].isdigit()


def test_every_category_has_entries():
    counts = count_by_category()
    assert set(counts) == set(CATEGORIES)
    assert all(value > 0 for value in counts.values())


def test_search_by_category_and_keyword():
    proxy = search(category="代理与网络")
    assert proxy
    assert all(item.category == "代理与网络" for item in proxy)

    assert search(category="代理与网络", keyword="PIP") != []

    matches = search(keyword="java")
    assert any(item.key == "JAVA_HOME" for item in matches)
    assert search(keyword="zzzz-not-exist") == []


def test_get_is_case_insensitive():
    assert get("java_home").key == "JAVA_HOME"
    assert get("  JAVA_HOME  ").key == "JAVA_HOME"
    assert get("NOPE") is None
    assert get("") is None


def test_sensitive_flags_follow_key_rules():
    for item in all_presets():
        assert item.sensitive == looks_sensitive_key(item.key)
    assert get("AWS_SECRET_ACCESS_KEY").sensitive
    assert get("DATABASE_URL").sensitive
    assert get("MONGO_URI").sensitive
    assert not get("JAVA_HOME").sensitive
