"""Redis-backed global counting semaphore for concurrent OCR-model calls.

Background:
    Chandra OCR fans a single document's pages out to a thread pool (see
    ``app/services/ingestion/chandra_extractor.py``), but historically that
    was the *only* source of OCR parallelism — the `ingest` Celery worker ran
    at ``--concurrency=1``, so documents were extracted one at a time. For a
    corpus dominated by 1-3 page letters, a lone document can't fan out at
    all, so OCR ran almost fully serially.

    This module lets the `ingest` worker run at a higher concurrency (many
    documents in flight) while still capping the *total* number of concurrent
    OCR-model HTTP calls across all of them at a configured N (the "OCR
    Concurrency" Settings knob) — so N one-page documents saturate N slots,
    and a one-page + an eight-page document split N as 1 + (N-1).

Design choices (mirrors ``app/services/model_gate.py``):
    - Each holder writes its own UUID sentinel key with a TTL well above the
      longest expected page call, so a crashed worker's slot self-heals when
      the sentinel expires — no explicit crash-recovery logic needed.
    - Atomic admit via a Redis Lua script: count live sentinels, compare
      against the configured limit, write our sentinel iff under the limit.
      Lua-script atomicity removes the COUNT->SET race between concurrent
      acquirers.
    - The limit is a plain Redis key (``sanctuary:ocr_slot:limit``), set by
      the Settings route and seeded at worker boot, so it survives worker
      restarts without a Settings save. A *missing* limit key is a normal
      Redis outcome (not a RedisError) and must never be treated as 0 — that
      would make every acquire compare "count < 0" and OCR would silently
      deadlock forever. The Lua coalesces a missing/unparseable limit to
      ``DEFAULT_OCR_CONCURRENCY``.
    - Redis unavailability degrades gracefully — log a warning once per
      minute and proceed without gating, matching ``model_gate.py`` and
      ``ai_inflight.py``. Worst case with Redis down: each of the (also
      Redis-gated-fail-open) `ingest` workers runs its per-document pool at
      up to N threads, so total concurrency is bounded by
      ``ingest_concurrency * N`` rather than unbounded.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import Generator

import redis

from app.config import AI_READ_TIMEOUT, REDIS_URL

logger = logging.getLogger(__name__)

_KEY_PREFIX = "sanctuary:ocr_slot:"
_CALL_KEY_PREFIX = _KEY_PREFIX + "call:"
_LIMIT_KEY = _KEY_PREFIX + "limit"

# Fallback used only if the limit key is missing/unparseable AND the caller
# didn't specify one explicitly — should not normally be hit once the worker
# entrypoint seeds the key at boot. Kept in sync with
# user_settings_service.DEFAULT_OCR_CONCURRENCY by convention, not import, to
# avoid a service->service import cycle; both default to 4.
DEFAULT_OCR_CONCURRENCY = 4

# Same headroom logic as model_gate.py — a sentinel outlives the longest
# plausible single-page HTTP call so a slow-but-live request is never
# prematurely evicted.
_SENTINEL_TTL_SECONDS = int(AI_READ_TIMEOUT) + 120

# Bounded above the longest expected wait for a slot to free up during a
# large batch ingest.
_DEFAULT_ACQUIRE_TIMEOUT = 30 * 60.0  # 30 min

_BACKOFF_INITIAL = 0.05
_BACKOFF_MAX = 2.0
_BACKOFF_GROWTH = 1.6

_last_warn_at: float = 0.0
_WARN_INTERVAL = 60.0


def _maybe_warn(exc: Exception) -> None:
    global _last_warn_at
    now = time.monotonic()
    if now - _last_warn_at >= _WARN_INTERVAL:
        logger.warning(
            "ocr_slots: Redis unavailable (%s); proceeding without gating", exc
        )
        _last_warn_at = now


# ---------------------------------------------------------------------------
# Lua script — atomic count + admit.
# ---------------------------------------------------------------------------
#
# KEYS[1] = sentinel key for this acquire attempt
# KEYS[2] = limit key
# ARGV[1] = sentinel TTL (seconds)
# ARGV[2] = default limit, used iff the limit key is missing/unparseable
#
# Returns:
#    1  → admitted (sentinel written)
#    0  → blocked (count already at or above the limit)
_ACQUIRE_LUA = """
local sentinel_key = KEYS[1]
local limit_key = KEYS[2]
local ttl = tonumber(ARGV[1])
local default_limit = tonumber(ARGV[2])

local limit = tonumber(redis.call("GET", limit_key)) or default_limit

local count = 0
local cursor = "0"
local match = "{prefix}*"
repeat
    local result = redis.call("SCAN", cursor, "MATCH", match, "COUNT", 100)
    cursor = result[1]
    for _, key in ipairs(result[2]) do
        if key ~= sentinel_key then
            count = count + 1
        end
    end
until cursor == "0"

if count >= limit then
    return 0
end

redis.call("SET", sentinel_key, "1", "EX", ttl)
return 1
""".replace("{prefix}", _CALL_KEY_PREFIX)


# ---------------------------------------------------------------------------
# Redis client / script handles — lazy singletons.
# ---------------------------------------------------------------------------

_sync_client: redis.Redis | None = None
_acquire_script: redis.commands.core.Script | None = None


def _get_client() -> redis.Redis:
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(
            REDIS_URL,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            decode_responses=True,
        )
    return _sync_client


def _get_acquire_script(client: redis.Redis) -> redis.commands.core.Script:
    global _acquire_script
    if _acquire_script is None:
        _acquire_script = client.register_script(_ACQUIRE_LUA)
    return _acquire_script


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def set_limit(n: int) -> None:
    """Publish the current OCR-slot limit to Redis.

    Called by the Settings route on save and by the worker-ingest entrypoint
    at boot (so the limit key exists before the first acquire, even if
    Redis was restarted since the last Settings save). Best-effort: swallows
    Redis errors like every other operation in this module.
    """
    try:
        _get_client().set(_LIMIT_KEY, str(n))
    except (redis.RedisError, OSError) as exc:
        _maybe_warn(exc)


@contextlib.contextmanager
def ocr_slot(
    *,
    timeout: float = _DEFAULT_ACQUIRE_TIMEOUT,
    label: str | None = None,
) -> Generator[str | None, None, None]:
    """Acquire one of the global OCR-model concurrency slots for the block.

    Args:
        timeout: Max seconds to wait for a free slot before raising
            TimeoutError.
        label: Optional short string for logging (e.g. "doc:42:page:3").

    Yields:
        The sentinel key on success (or ``None`` when Redis is unavailable
        and the gate is degrading open). Callers don't need the token.

    Behaviour when Redis is unreachable: log a warning at most once per
    minute and proceed without gating — same fail-open semantics as
    ``model_gate.py``.
    """
    call_id = uuid.uuid4().hex
    sentinel_key = _CALL_KEY_PREFIX + call_id
    acquired = False

    deadline = time.monotonic() + timeout
    backoff = _BACKOFF_INITIAL
    wait_logged = False
    started_wait_at: float | None = None

    try:
        client = _get_client()
        script = _get_acquire_script(client)

        while True:
            try:
                result = script(
                    keys=[sentinel_key, _LIMIT_KEY],
                    args=[_SENTINEL_TTL_SECONDS, DEFAULT_OCR_CONCURRENCY],
                )
            except (redis.RedisError, OSError) as exc:
                _maybe_warn(exc)
                # Degrade open: skip gating but still let the call proceed.
                yield None
                return

            if int(result) == 1:
                acquired = True
                if started_wait_at is not None:
                    waited = time.monotonic() - started_wait_at
                    logger.info(
                        "ocr_slots: %s acquired a slot after %.1fs wait",
                        label or "<unlabeled>",
                        waited,
                    )
                yield sentinel_key
                return

            # Blocked — all slots in use. Wait and retry.
            now = time.monotonic()
            if started_wait_at is None:
                started_wait_at = now
            if not wait_logged:
                logger.info(
                    "ocr_slots: %s waiting for a free OCR slot",
                    label or "<unlabeled>",
                )
                wait_logged = True
            if now >= deadline:
                raise TimeoutError(
                    f"ocr_slots: timed out after {timeout:.0f}s waiting for a "
                    f"free slot (label={label or '<unlabeled>'})"
                )
            time.sleep(min(backoff, max(0.0, deadline - now)))
            backoff = min(backoff * _BACKOFF_GROWTH, _BACKOFF_MAX)
    finally:
        if acquired:
            try:
                _get_client().delete(sentinel_key)
            except (redis.RedisError, OSError) as exc:
                _maybe_warn(exc)


# ---------------------------------------------------------------------------
# Diagnostics — surfaced by the settings UI or for ad-hoc inspection.
# ---------------------------------------------------------------------------


def inflight_count() -> int:
    """Return the number of OCR slots currently held, across all workers.

    Returns 0 (not raises) on Redis errors so callers can render diagnostic
    UIs without 500s.
    """
    try:
        client = _get_client()
        return sum(1 for _ in client.scan_iter(match=_CALL_KEY_PREFIX + "*", count=100))
    except (redis.RedisError, OSError) as exc:
        _maybe_warn(exc)
        return 0
