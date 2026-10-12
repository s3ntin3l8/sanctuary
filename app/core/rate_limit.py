"""Shared rate-limiter instance.

Defined here (not in app.main) so routers can import it without creating
a circular dependency: app.main → app.api → routers → app.main.
"""

import logging
import os
import time

import redis
from limits import parse
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import RATE_LIMIT_STORAGE_URI

logger = logging.getLogger(__name__)

# Counters live in Redis so every uvicorn worker shares one budget (and a
# restart does not reset it). ``memory://`` keeps them in-process (tests).
# Short socket timeouts: a dead Redis must cost milliseconds, not a hang.
_STORAGE_OPTIONS = (
    {"socket_connect_timeout": 0.5, "socket_timeout": 0.5}
    if RATE_LIMIT_STORAGE_URI.startswith(("redis", "rediss"))
    else {}
)

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["600/minute"],
    storage_uri=RATE_LIMIT_STORAGE_URI,
    storage_options=_STORAGE_OPTIONS,
    in_memory_fallback_enabled=True,
)

# Warn at most once per minute when the limiter storage is unreachable.
_last_warn_at: float = 0.0
_WARN_INTERVAL = 60.0


def _maybe_warn(exc: Exception) -> None:
    global _last_warn_at
    now = time.monotonic()
    if now - _last_warn_at >= _WARN_INTERVAL:
        logger.warning("rate_limit: storage unavailable (%s), failing open", exc)
        _last_warn_at = now


def _hit(strategy: FixedWindowRateLimiter, rate, *identifiers: str) -> bool:
    """Count one hit; a storage outage allows the call (fail open) instead of 500."""
    try:
        return strategy.hit(rate, *identifiers)
    except (redis.RedisError, OSError) as exc:
        _maybe_warn(exc)
        return True


# Per-token budget for the MCP endpoint. slowapi keys on client IP, which is
# the wrong identity for an agent: many tokens can share one IP and one token
# can roam. Uses the `limits` package slowapi is built on, on the shared
# Redis storage so the budget holds across workers.
MCP_TOKEN_RATE_LIMIT = parse(os.getenv("MCP_TOKEN_RATE_LIMIT", "120/minute"))
_token_limiter = FixedWindowRateLimiter(
    storage_from_string(RATE_LIMIT_STORAGE_URI, **_STORAGE_OPTIONS)
)


def hit_token_limit(token_id: int) -> bool:
    """Count one call for ``token_id``; False once its window budget is spent."""
    return _hit(_token_limiter, MCP_TOKEN_RATE_LIMIT, "mcp-token", str(token_id))


# Per-client-IP budgets for the unauthenticated OAuth endpoints (/register,
# /token): machine-to-machine calls with no session to key on.
OAUTH_REGISTER_LIMIT = parse("20/hour")
OAUTH_TOKEN_LIMIT = parse("120/minute")
_ip_limiter = FixedWindowRateLimiter(
    storage_from_string(RATE_LIMIT_STORAGE_URI, **_STORAGE_OPTIONS)
)


def hit_ip_limit(name: str, rate, ip: str) -> bool:
    """Count one call to endpoint ``name`` from ``ip``; False once over budget."""
    return _hit(_ip_limiter, rate, name, ip)
