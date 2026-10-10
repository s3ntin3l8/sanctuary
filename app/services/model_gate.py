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
    - Fairness: a blocked acquirer leaves a wait marker; once an incompatible
      waiter has waited ``_FAIRNESS_AFTER_SECONDS``, new acquirers of the
      holding family are refused so the gate can drain and flip (otherwise
      overlapping same-family calls starve the waiter for its full timeout).
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
import threading
import time
import uuid
from collections.abc import Generator
from typing import cast

import redis

from app.config import AI_READ_TIMEOUT, REDIS_URL

logger = logging.getLogger(__name__)


class ModelGateTimeout(TimeoutError):
    """Waited the full acquire timeout for the model slot (another family held it).

    A TimeoutError subclass so existing handlers keep working, but distinct from
    a network/read timeout: the model itself was never called, so retrying the
    task later is safe and usually succeeds once the other family drains.
    """


_KEY_PREFIX = "sanctuary:model_gate:"
_CALL_KEY_PREFIX = _KEY_PREFIX + "call:"
_WAIT_KEY_PREFIX = _KEY_PREFIX + "wait:"

# Fairness: a blocked acquirer leaves a wait marker. Once an incompatible
# waiter has been blocked this long, *new* acquirers of the family that holds
# the gate are refused too, so the in-flight calls drain and the gate flips.
# Without it, "same family runs in parallel" lets a steady stream of qwen
# calls (3 ai workers) keep the gate occupied forever while a chandra OCR
# call waits out its whole 30 min timeout.
_FAIRNESS_AFTER_SECONDS = 120

# A wait marker is refreshed on every poll (<= _BACKOFF_MAX); the TTL only
# matters when a waiter dies without cleaning up.
_WAIT_KEY_TTL_SECONDS = 30

# _ACQUIRE_LUA's "blocked" result when only fairness (not a call in flight)
# refuses the acquire; 0 is the ordinary conflict, 1 is acquired.
_BLOCKED_BY_FAIRNESS = 2

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
# KEYS[2] = this acquirer's wait-marker key
# ARGV[1] = family being acquired
# ARGV[2] = sentinel TTL (seconds)
# ARGV[3] = fairness threshold (seconds an incompatible waiter must have waited)
# ARGV[4] = wait-marker TTL (seconds)
# ARGV[5..N] = compatible-families list (the keys of the family's compat set)
#
# Returns:
#    1  → acquired (sentinel written, wait marker cleared)
#    0  → blocked (an incompatible family is in flight)
#    2  → blocked by fairness (nothing incompatible need be in flight: an
#         incompatible waiter older than us has waited past the threshold)
_ACQUIRE_LUA = """
local sentinel_key = KEYS[1]
local wait_key = KEYS[2]
local family = ARGV[1]
local ttl = tonumber(ARGV[2])
local fairness = tonumber(ARGV[3])
local wait_ttl = tonumber(ARGV[4])
local compat = {}
for i = 5, #ARGV do
    compat[ARGV[i]] = true
end

local now = tonumber(redis.call("TIME")[1])
local own = redis.call("GET", wait_key)
local my_ts = nil
if own then
    my_ts = tonumber(string.match(own, "|(%d+)$"))
end

local blocked = false
local reason = 0

local cursor = "0"
repeat
    local result = redis.call("SCAN", cursor, "MATCH", "{wait_prefix}*", "COUNT", 100)
    cursor = result[1]
    for _, key in ipairs(result[2]) do
        if key ~= wait_key then
            local v = redis.call("GET", key)
            if v then
                local f, ts = string.match(v, "^(.-)|(%d+)$")
                ts = tonumber(ts)
                if f and ts and not compat[f] and now - ts >= fairness
                    and (my_ts == nil or ts < my_ts) then
                    blocked = true
                    reason = 2
                end
            end
        end
    end
until cursor == "0"

if not blocked then
    cursor = "0"
    repeat
        local result = redis.call("SCAN", cursor, "MATCH", "{call_prefix}*", "COUNT", 100)
        cursor = result[1]
        for _, key in ipairs(result[2]) do
            if key ~= sentinel_key then
                local f = redis.call("GET", key)
                if f and not compat[f] then
                    blocked = true
                end
            end
        end
    until cursor == "0"
end

if blocked then
    if own then
        redis.call("EXPIRE", wait_key, wait_ttl)
    else
        redis.call("SET", wait_key, family .. "|" .. now, "EX", wait_ttl)
    end
    return reason
end

redis.call("DEL", wait_key)
redis.call("SET", sentinel_key, family, "EX", ttl)
return 1
""".replace("{call_prefix}", _CALL_KEY_PREFIX).replace(
    "{wait_prefix}", _WAIT_KEY_PREFIX
)


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


# Heartbeat: without this, a held gate's sentinel TTL (_SENTINEL_TTL_SECONDS,
# ~AI_READ_TIMEOUT + 120s) can lapse mid-call on a document whose actual work
# — e.g. many chandra OCR pages, each waiting its own turn on ocr_slot() —
# legitimately runs far longer than a single HTTP call's timeout. Once the
# sentinel expires, a waiting qwen acquirer is free to steal the gate out
# from under the still-running chandra call. Refresh at most every TTL/3 so
# at least two heartbeats land before expiry even if one is delayed.
_HEARTBEAT_INTERVAL_DIVISOR = 3


def _run_heartbeat(
    sentinel_key: str,
    family: str,
    label: str | None,
    stop: threading.Event,
    interval: float,
) -> None:
    """Background thread body: periodically refresh sentinel_key's TTL until
    `stop` is set. The sentinel key is a per-call UUID (see model_gate), so
    refreshing our own key has no ownership race with any other call."""
    while not stop.wait(interval):
        try:
            refreshed = _get_client().expire(sentinel_key, _SENTINEL_TTL_SECONDS)
            if not refreshed:
                logger.warning(
                    "model_gate: heartbeat found %s's %s sentinel already "
                    "expired — it may have lost the gate mid-call",
                    label or "<unlabeled>",
                    family,
                )
                return
        except (redis.RedisError, OSError) as exc:
            _maybe_warn(exc)


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
    wait_key = _WAIT_KEY_PREFIX + call_id
    acquired = False
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None

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
                    # redis-py's CoreCommands mixin is shared between the
                    # sync and async clients, so its stubs type llen() as
                    # returning Awaitable[int] | int even though this is
                    # the sync client and always returns a plain int here.
                    pending_ingest = cast(int, client.llen(_INGEST_QUEUE_NAME))
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
                            raise ModelGateTimeout(
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
                    keys=[sentinel_key, wait_key],
                    args=[
                        family,
                        _SENTINEL_TTL_SECONDS,
                        _FAIRNESS_AFTER_SECONDS,
                        _WAIT_KEY_TTL_SECONDS,
                        *compat_list,
                    ],
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
                heartbeat_stop = threading.Event()
                heartbeat_thread = threading.Thread(
                    target=_run_heartbeat,
                    args=(
                        sentinel_key,
                        family,
                        label,
                        heartbeat_stop,
                        _SENTINEL_TTL_SECONDS / _HEARTBEAT_INTERVAL_DIVISOR,
                    ),
                    daemon=True,
                    name=f"model_gate-heartbeat-{family}",
                )
                heartbeat_thread.start()
                yield sentinel_key
                return

            # Blocked — another family is in flight. Wait and retry.
            now = time.monotonic()
            if started_wait_at is None:
                started_wait_at = now
            if not wait_logged:
                logger.info(
                    "model_gate: %s waiting for %s (%s)",
                    label or "<unlabeled>",
                    family,
                    (
                        "an incompatible waiter has priority"
                        if int(result) == _BLOCKED_BY_FAIRNESS
                        else "another family holds the gate"
                    ),
                )
                wait_logged = True
            if now >= deadline:
                raise ModelGateTimeout(
                    f"model_gate: timed out after {timeout:.0f}s waiting for {family} "
                    f"(label={label or '<unlabeled>'})"
                )
            time.sleep(min(backoff, max(0.0, deadline - now)))
            backoff = min(backoff * _BACKOFF_GROWTH, _BACKOFF_MAX)
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2.0)
        if not acquired:
            # Timed out / interrupted while blocked: drop our wait marker so
            # it stops holding off other acquirers.
            try:
                _get_client().delete(wait_key)
            except (redis.RedisError, OSError) as exc:
                _maybe_warn(exc)
        if acquired:
            try:
                _get_client().delete(sentinel_key)
            except (redis.RedisError, OSError) as exc:
                _maybe_warn(exc)
