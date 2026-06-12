"""Unit tests for the in-memory TTL cache (app/core/cache.py)."""

import pytest

from app.core.cache import (
    TTLCache,
    get_ai_summary_key,
    get_case_detail_key,
)


@pytest.mark.unit
def test_set_then_get_roundtrip():
    c = TTLCache()
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}


@pytest.mark.unit
def test_get_missing_returns_none():
    assert TTLCache().get("absent") is None


@pytest.mark.unit
def test_expired_entry_is_evicted_on_get():
    c = TTLCache()
    c.set("k", "v", ttl=-1)  # already expired
    assert c.get("k") is None
    # eviction happened — key is gone from the backing store
    assert "k" not in c._cache


@pytest.mark.unit
def test_default_ttl_is_used_when_unspecified():
    c = TTLCache(default_ttl=300)
    c.set("k", "v")
    assert c.get("k") == "v"


@pytest.mark.unit
def test_delete_returns_true_then_false():
    c = TTLCache()
    c.set("k", "v")
    assert c.delete("k") is True
    assert c.delete("k") is False


@pytest.mark.unit
def test_clear_removes_everything():
    c = TTLCache()
    c.set("a", 1)
    c.set("b", 2)
    c.clear()
    assert c.get("a") is None
    assert c.get("b") is None


@pytest.mark.unit
def test_cleanup_expired_removes_only_expired():
    c = TTLCache()
    c.set("fresh", 1, ttl=300)
    c.set("stale", 2, ttl=-1)
    removed = c.cleanup_expired()
    assert removed == 1
    assert c.get("fresh") == 1
    assert c.get("stale") is None


@pytest.mark.unit
def test_key_helpers_format_ids():
    assert get_ai_summary_key(42) == "ai_summary:42"
    assert get_case_detail_key("ADV-024-A") == "case_detail:ADV-024-A"
