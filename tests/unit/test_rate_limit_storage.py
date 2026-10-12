"""Rate-limit storage: built from the configured URI, fails open when it is down."""

from limits import parse
from limits.storage import MemoryStorage, RedisStorage, storage_from_string
from limits.strategies import FixedWindowRateLimiter

from app.core import rate_limit


def test_storage_is_built_from_the_uri():
    # Tests run with RATE_LIMIT_STORAGE_URI=memory:// (tests/conftest.py).
    assert isinstance(rate_limit._token_limiter.storage, MemoryStorage)
    assert isinstance(rate_limit._ip_limiter.storage, MemoryStorage)
    assert isinstance(
        storage_from_string("redis://127.0.0.1:1/0", **rate_limit._STORAGE_OPTIONS),
        RedisStorage,
    )


def test_unreachable_redis_fails_open(monkeypatch):
    dead = FixedWindowRateLimiter(
        storage_from_string(
            "redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2
        )
    )
    monkeypatch.setattr(rate_limit, "_token_limiter", dead)
    monkeypatch.setattr(rate_limit, "_ip_limiter", dead)
    monkeypatch.setattr(rate_limit, "_last_warn_at", 0.0)

    assert rate_limit.hit_token_limit(1) is True
    assert rate_limit.hit_ip_limit("register", parse("1/minute"), "1.2.3.4") is True
    assert rate_limit.hit_ip_limit("register", parse("1/minute"), "1.2.3.4") is True
