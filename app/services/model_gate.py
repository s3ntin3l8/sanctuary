"""Redis-backed model-family gate for coordinating GPU-shared inference.

Background:
    The configured inference host (e.g. LMStudio at the litellm proxy) cannot
    hold the Chandra OCR model and the Qwen chat model in VRAM at the same
    time — they swap on demand. Without coordination, the Celery `ingest`
    worker (Chandra OCR) and `ai` worker (Qwen enrichment) pull tasks in
    parallel during batch ingest and force LMStudio to thrash, with each
    cross-model handoff costing tens of seconds.

    This service exposes a sync context manager — ``with model_gate(family):``
    — that callers wrap around their actual HTTP call. Same-family calls
    proceed in parallel (vLLM batches them on the loaded model); a call for
    a different incompatible family blocks until the in-flight family
    drains. Embeddings (nomic-embed-text class) are small and treated as
    compatible with everything; this keeps the call sites uniform while
    leaving room to tighten the policy later by editing only the
    ``COMPATIBILITY`` table.

Design choices:
    - Atomic acquire via a Redis Lua script: SCAN existing per-call
      sentinels, reject if any are incompatible with our family, otherwise
      write our sentinel. Lua-script atomicity removes the SCAN→SET race.
    - Crash recovery is automatic: every sentinel carries a TTL well above
      ``AI_READ_TIMEOUT``, so a worker that dies without releasing is
      reclaimed when its key expires.
    - Wait loop: simple polling with capped exponential backoff. A pub-sub
      wakeup channel would be slightly faster but adds infra complexity
      that's not justified at single-user batch scale.
    - Redis unavailability degrades gracefully — the gate logs a warning
      once per minute and proceeds without blocking, matching the
      ``ai_inflight.py`` pattern.
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

_KEY_PREFIX = "sanctuary:model_gate:"
_CALL_KEY_PREFIX = _KEY_PREFIX + "call:"

# Each per-call sentinel lives slightly longer than the longest plausible
# HTTP call so a slow-but-live request never has its key prematurely
# evicted. Mirrors the headroom logic in ai_inflight.py.
_SENTINEL_TTL_SECONDS = int(AI_READ_TIMEOUT) + 120

# Default acquire timeout — bounded above the longest expected
# cross-family wait (a chandra extraction of a 20+ page doc can run for
# several minutes, and a queued qwen waiter must outlast that).
_DEFAULT_ACQUIRE_TIMEOUT = 30 * 60.0  # 30 min

# Polling cadence between try-acquire attempts when a different family
# holds the gate. Caps at 2s so a freshly-released gate doesn't keep
# waiters parked for long.
_BACKOFF_INITIAL = 0.05
_BACKOFF_MAX = 2.0
_BACKOFF_GROWTH = 1.6

# Celery queue that carries chandra OCR work (process_document_task). When
# a qwen acquirer would otherwise be granted the freshly-released gate,
# but the ingest queue still has pending chandra tasks, qwen defers so
# the ingest worker can keep the chandra slot hot across the batch. This
# is the "drain extract first" bias that prevents per-doc model swaps
# during batch ingest.
#
# Since the OCR->chat barrier (claim_batch_for_metadata_phase, see
# app/services/intelligence/orchestrator.py) was added, intra-batch model
# swapping is eliminated by construction — metadata_task no longer dispatches
# until every doc in the batch has a terminal EXTRACT, so there's rarely a
# qwen call in flight while chandra work for the *same* batch remains. This
# bias is still load-bearing as the *cross-batch* backstop (batch B's OCR vs.
# batch A's chat racing) and as a fallback if the barrier's dispatch is ever
# delayed — but its intra-batch leaks (the 10-min defer cap below, the
# queue-length blind spot on in-flight/prefetched tasks, and fail-open on
# Redis errors) are no longer the primary defense, so they're left as-is
# rather than hardened further.
_INGEST_QUEUE_NAME = "ingest"

# Maximum total time a single qwen acquire will defer for the ingest
# queue before falling back to normal acquire semantics. Bounds the
# starvation risk if gmail-sync or scan-folder keep feeding new work
# faster than chandra can drain it. The overall acquire timeout
# (_DEFAULT_ACQUIRE_TIMEOUT, 30 min) still applies on top of this.
_QUEUE_DEFER_CAP_SECONDS = 10 * 60.0  # 10 min

# Compatibility table — keys are the family being acquired, values are the
# set of families that may coexist in flight. chandra ↔ qwen exclude each
# other; embed (small enough to coexist) is compatible with everything.
# Change this table to update policy; call sites stay uniform.
COMPATIBILITY: dict[str, frozenset[str]] = {
    "chandra": frozenset({"chandra", "embed"}),
    "qwen": frozenset({"qwen", "embed"}),
    "embed": frozenset({"chandra", "qwen", "embed"}),
}

_VALID_FAMILIES = frozenset(COMPATIBILITY)


# ---------------------------------------------------------------------------
# Lua script — atomic compatibility check + sentinel write.
# ---------------------------------------------------------------------------
#
# KEYS[1] = sentinel key for this acquire attempt
# ARGV[1] = family being acquired
# ARGV[2] = sentinel TTL (seconds)
# ARGV[3..N] = compatible-families list (the keys of the family's compat set)
#
# Returns:
#    1  → acquired (sentinel written)
#    0  → blocked (an incompatible family is currently in flight)
_ACQUIRE_LUA = """
local sentinel_key = KEYS[1]
local family = ARGV[1]
local ttl = tonumber(ARGV[2])
local compat = {}
for i = 3, #ARGV do
    compat[ARGV[i]] = true
end

local cursor = "0"
local match = "{prefix}*"
repeat
    local result = redis.call("SCAN", cursor, "MATCH", match, "COUNT", 100)
    cursor = result[1]
    for _, key in ipairs(result[2]) do
        if key ~= sentinel_key then
            local f = redis.call("GET", key)
            if f and not compat[f] then
                return 0
            end
        end
    end
until cursor == "0"

redis.call("SET", sentinel_key, family, "EX", ttl)
return 1
""".replace("{prefix}", _CALL_KEY_PREFIX)


# ---------------------------------------------------------------------------
# Redis client / script handles — lazy singletons.
# ---------------------------------------------------------------------------

_sync_client: redis.Redis | None = None
_acquire_script: redis.commands.core.Script | None = None

_last_warn_at: float = 0.0
_WARN_INTERVAL = 60.0


def _maybe_warn(exc: Exception) -> None:
    global _last_warn_at
    now = time.monotonic()
    if now - _last_warn_at >= _WARN_INTERVAL:
        logger.warning(
            "model_gate: Redis unavailable (%s); proceeding without gating", exc
        )
        _last_warn_at = now


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


@contextlib.contextmanager
def model_gate(
    family: str,
    *,
    timeout: float = _DEFAULT_ACQUIRE_TIMEOUT,
    label: str | None = None,
) -> Generator[str | None, None, None]:
    """Acquire the gate for ``family`` for the duration of the with-block.

    Args:
        family: One of ``chandra``, ``qwen``, ``embed``. Unknown families
            raise ValueError so call sites can't silently desync from the
            compatibility policy.
        timeout: Max seconds to wait before raising TimeoutError.
        label: Optional short string for logging (e.g. ``"enrich:doc:42"``).

    Yields:
        The sentinel key on success (or ``None`` when Redis is unavailable
        and the gate is degrading open). Callers don't need the token.

    Behaviour when Redis is unreachable: log a warning at most once per
    minute and proceed without gating — same fail-open semantics as
    ``ai_inflight.track_ai_call``. The alternative (refusing to make AI
    calls when Redis is down) would block every ingestion pipeline in the
    app on a missing-but-unrelated dependency.
    """
    if family not in _VALID_FAMILIES:
        raise ValueError(
            f"model_gate: unknown family {family!r}; expected one of "
            f"{sorted(_VALID_FAMILIES)}"
        )

    call_id = uuid.uuid4().hex
    sentinel_key = _CALL_KEY_PREFIX + call_id
    acquired = False

    deadline = time.monotonic() + timeout
    backoff = _BACKOFF_INITIAL
    wait_logged = False
    queue_defer_logged = False
    queue_defer_exhausted = False  # latched True once cap reached; never re-check
    started_wait_at: float | None = None
    queue_defer_started_at: float | None = None

    try:
        client = _get_client()
        script = _get_acquire_script(client)
        compat_list = sorted(COMPATIBILITY[family])

        while True:
            # Drain-first bias: qwen yields to pending chandra work on the
            # ingest queue so a multi-doc batch keeps chandra loaded across
            # all extracts instead of swapping models after each one. Only
            # applies to qwen — chandra never defers, embed is compatible
            # with everyone so the check would be moot. Latched off once
            # the per-acquire defer cap is reached, so a perpetually-fed
            # ingest queue can't starve qwen forever.
            if family == "qwen" and not queue_defer_exhausted:
                try:
                    pending_ingest = client.llen(_INGEST_QUEUE_NAME)
                except (redis.RedisError, OSError) as exc:
                    _maybe_warn(exc)
                    pending_ingest = 0

                if pending_ingest > 0:
                    now = time.monotonic()
                    if queue_defer_started_at is None:
                        queue_defer_started_at = now
                    deferred_for = now - queue_defer_started_at
                    if deferred_for < _QUEUE_DEFER_CAP_SECONDS:
                        if not queue_defer_logged:
                            logger.info(
                                "model_gate: %s waiting for ingest queue "
                                "(%d task(s) pending — chandra stays loaded)",
                                label or "<unlabeled>",
                                pending_ingest,
                            )
                            queue_defer_logged = True
                        if started_wait_at is None:
                            started_wait_at = now
                        if now >= deadline:
                            raise TimeoutError(
                                f"model_gate: timed out after {timeout:.0f}s "
                                f"deferring for ingest queue "
                                f"(label={label or '<unlabeled>'})"
                            )
                        time.sleep(min(backoff, max(0.0, deadline - now)))
                        backoff = min(backoff * _BACKOFF_GROWTH, _BACKOFF_MAX)
                        continue
                    # Defer cap reached — log once, latch off, and fall
                    # through to normal acquire.
                    queue_defer_exhausted = True
                    logger.warning(
                        "model_gate: %s deferred %.0fs for ingest queue, "
                        "cap reached — falling back to normal acquire",
                        label or "<unlabeled>",
                        deferred_for,
                    )

            try:
                result = script(
                    keys=[sentinel_key],
                    args=[family, _SENTINEL_TTL_SECONDS, *compat_list],
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
                        "model_gate: %s acquired %s after %.1fs wait",
                        label or "<unlabeled>",
                        family,
                        waited,
                    )
                yield sentinel_key
                return

            # Blocked — another family is in flight. Wait and retry.
            now = time.monotonic()
            if started_wait_at is None:
                started_wait_at = now
            if not wait_logged:
                logger.info(
                    "model_gate: %s waiting for %s (another family holds the gate)",
                    label or "<unlabeled>",
                    family,
                )
                wait_logged = True
            if now >= deadline:
                raise TimeoutError(
                    f"model_gate: timed out after {timeout:.0f}s waiting for {family} "
                    f"(label={label or '<unlabeled>'})"
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


def inflight_by_family() -> dict[str, int]:
    """Return ``{family: count_in_flight}`` across all workers.

    Returns an empty dict (not raises) on Redis errors so callers can
    render diagnostic UIs without 500s.
    """
    out: dict[str, int] = {}
    try:
        client = _get_client()
        for key in client.scan_iter(match=_CALL_KEY_PREFIX + "*", count=100):
            family = client.get(key)
            if family:
                out[family] = out.get(family, 0) + 1
    except (redis.RedisError, OSError) as exc:
        _maybe_warn(exc)
    return out
