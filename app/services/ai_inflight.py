"""Redis-backed in-flight AI call counter.

Every active AI HTTP call registers a sentinel key in Redis with a TTL so that
crash-recovery is automatic: if a worker dies without hitting its finally-block
the key expires on its own. The UI reads count_inflight() to show a single
canonical "Active AI calls" number that covers all call types — doc-stage
tasks, case briefs, embeddings, and chat streams — across all processes.

Design: per-call keys (not a single INCR/DECR) so a crashed worker can't leak
a stuck counter indefinitely. TTL is set well above the configured AI_READ_TIMEOUT
so a slow-but-live call is never prematurely evicted.
"""

import contextlib
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Generator

import redis
import redis.asyncio as aioredis

from app.config import REDIS_URL

logger = logging.getLogger(__name__)

_KEY_PREFIX = "sanctuary:ai_inflight:"
_TTL_SECONDS = 600  # 10 min — safely above the 600s AI_READ_TIMEOUT default

# Warn at most once per minute to avoid log spam when Redis is unreachable.
_last_warn_at: float = 0.0
_WARN_INTERVAL = 60.0


def _maybe_warn(exc: Exception) -> None:
    global _last_warn_at
    now = time.monotonic()
    if now - _last_warn_at >= _WARN_INTERVAL:
        logger.warning("ai_inflight: Redis unavailable (%s), counter degraded", exc)
        _last_warn_at = now


# ---------------------------------------------------------------------------
# Sync client — lazy singleton (safe: no event-loop lifecycle concerns).
# Async client — created fresh per call so it doesn't outlive the event loop
# that created it (avoids "Event loop is closed" during test teardown).
# ---------------------------------------------------------------------------

_sync_client: redis.Redis | None = None


def _get_sync_client() -> redis.Redis:
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(
            REDIS_URL,
            socket_timeout=0.1,
            socket_connect_timeout=0.1,
            decode_responses=True,
        )
    return _sync_client


def _new_async_client() -> aioredis.Redis:
    return aioredis.Redis.from_url(
        REDIS_URL,
        socket_timeout=0.1,
        socket_connect_timeout=0.1,
        decode_responses=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def track_ai_call(label: str) -> Generator[None, None, None]:
    """Sync context manager — wraps a blocking AI HTTP call (Celery workers).

    Usage::

        with track_ai_call("enrich:doc:42"):
            response = client.post(...)
    """
    key = _KEY_PREFIX + str(uuid.uuid4())
    registered = False
    try:
        _get_sync_client().set(key, label, ex=_TTL_SECONDS)
        registered = True
    except (redis.RedisError, OSError) as exc:
        _maybe_warn(exc)
    try:
        yield
    finally:
        if registered:
            try:
                _get_sync_client().delete(key)
            except (redis.RedisError, OSError) as exc:
                _maybe_warn(exc)


@contextlib.asynccontextmanager
async def track_ai_call_async(label: str) -> AsyncGenerator[None, None]:
    """Async context manager — wraps an async AI HTTP call (FastAPI / async tasks).

    A fresh Redis client is created and closed per call so the client never
    outlives the event loop that created it (avoids "Event loop is closed"
    during test teardown).

    Usage::

        async with track_ai_call_async("chat:case:ADV-024-A"):
            async with httpx.AsyncClient() as client:
                ...
    """
    client = _new_async_client()
    key = _KEY_PREFIX + str(uuid.uuid4())
    registered = False
    try:
        try:
            await client.set(key, label, ex=_TTL_SECONDS)
            registered = True
        except (aioredis.RedisError, OSError) as exc:
            _maybe_warn(exc)
        try:
            yield
        finally:
            if registered:
                try:
                    await client.delete(key)
                except (aioredis.RedisError, OSError) as exc:
                    _maybe_warn(exc)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


def count_inflight() -> int:
    """Return the number of currently active AI calls across all processes.

    Returns 0 (not raises) if Redis is unreachable so the UI degrades
    gracefully rather than surfacing 500s.
    """
    try:
        client = _get_sync_client()
        return sum(1 for _ in client.scan_iter(match=_KEY_PREFIX + "*", count=100))
    except (redis.RedisError, OSError) as exc:
        _maybe_warn(exc)
        return 0
