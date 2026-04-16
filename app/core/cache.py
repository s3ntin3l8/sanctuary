import threading
import time
from typing import Any


class TTLCache:
    """Thread-safe in-memory cache with TTL support."""

    def __init__(self, default_ttl: int = 300):
        """
        Initialize cache.

        Args:
            default_ttl: Default time-to-live in seconds (default: 5 minutes)
        """
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        """
        Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        with self._lock:
            if key in self._cache:
                value, expires_at = self._cache[key]
                if time.time() < expires_at:
                    return value
                del self._cache[key]
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """
        Set value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds (uses default if None)
        """
        ttl = ttl if ttl is not None else self._default_ttl
        expires_at = time.time() + ttl
        with self._lock:
            self._cache[key] = (value, expires_at)

    def delete(self, key: str) -> bool:
        """
        Delete value from cache.

        Args:
            key: Cache key

        Returns:
            True if key was deleted, False if not found
        """
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> None:
        """Clear all cached values."""
        with self._lock:
            self._cache.clear()

    def cleanup_expired(self) -> int:
        """
        Remove all expired entries.

        Returns:
            Number of entries removed
        """
        now = time.time()
        removed = 0
        with self._lock:
            expired_keys = [k for k, (_, exp) in self._cache.items() if now >= exp]
            for key in expired_keys:
                del self._cache[key]
                removed += 1
        return removed


# Global cache instance with 5-minute default TTL
cache = TTLCache(default_ttl=300)

# Cache keys constants
CACHE_KEY_AI_SUMMARY = "ai_summary:{doc_id}"
CACHE_KEY_CASE_DIRECTORY = "case_directory"
CACHE_KEY_CASE_DETAIL = "case_detail:{case_id}"


def get_ai_summary_key(doc_id: int) -> str:
    """Get cache key for AI summary."""
    return f"ai_summary:{doc_id}"


def get_case_detail_key(case_id: str) -> str:
    """Get cache key for case detail."""
    return f"case_detail:{case_id}"
