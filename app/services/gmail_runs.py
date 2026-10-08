"""Progress/control state for long-running Gmail jobs (index refresh, import).

One JSON blob per (kind, user) in Redis. The Celery task reads and writes this
state as it goes, so it is also the control channel: ``cancel_run`` flips it and
the task stops at its next write.

Every write is an optimistic read-modify-write (WATCH/MULTI) keyed on ``run_id``,
so:

* a cancel can never be overwritten by a task that read the state earlier;
* a stale task of an old run can never clobber a newer run.

A run that stops heartbeating (``updated_at``) is considered abandoned, so a lost
task cannot lock the user out until the Redis key expires.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import redis

from app.config import REDIS_URL
from app.core.timezone import now_utc

logger = logging.getLogger(__name__)

RunKind = Literal["index", "import"]

_ACTIVE_TTL_SECONDS = 24 * 3600
_FINISHED_TTL_SECONDS = 3600
# A live run touches its state at least every ~15s (index chunk / imported message). No
# touch for this long means the task that owned it is gone.
_STALE_SECONDS = 600
_MAX_CONTENTION_RETRIES = 5

_client: redis.Redis | None = None


class RunStateUnavailable(RuntimeError):
    """Redis could not be reached or kept losing the write race."""


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            decode_responses=True,
        )
    return _client


def _key(kind: RunKind, user_id: int) -> str:
    return f"sanctuary:gmail_{kind}:{user_id}"


def get_run(kind: RunKind, user_id: int) -> dict[str, Any] | None:
    try:
        raw = _get_client().get(_key(kind, user_id))
    except (redis.RedisError, OSError):
        return None
    return json.loads(raw) if raw else None


def is_active(state: dict[str, Any] | None) -> bool:
    return bool(state) and not state.get("finished_at")  # type: ignore[union-attr]


def is_live(state: dict[str, Any] | None) -> bool:
    """Active *and* heard from recently — i.e. a task is actually working on it."""
    if not is_active(state):
        return False
    assert state is not None
    updated = state.get("updated_at")
    if not updated:
        return True
    age = now_utc() - datetime.fromisoformat(updated)
    return age.total_seconds() < _STALE_SECONDS


def _transact(
    kind: RunKind,
    user_id: int,
    fn: Callable[[dict[str, Any] | None], tuple[dict[str, Any] | None, Any]],
) -> Any:
    """Apply ``fn(current) -> (new_state | None, result)`` atomically.

    ``None`` as the new state leaves the key untouched and just returns
    ``result``. Retried when another writer changed the key mid-flight.
    """
    key = _key(kind, user_id)
    try:
        with _get_client().pipeline() as pipe:
            for _ in range(_MAX_CONTENTION_RETRIES):
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    new_state, result = fn(json.loads(raw) if raw else None)
                    if new_state is None:
                        pipe.unwatch()
                        return result
                    new_state = {**new_state, "updated_at": now_utc().isoformat()}
                    ttl = (
                        _FINISHED_TTL_SECONDS
                        if new_state.get("finished_at")
                        else _ACTIVE_TTL_SECONDS
                    )
                    pipe.multi()
                    pipe.set(key, json.dumps(new_state), ex=ttl)
                    pipe.execute()
                    return result
                except redis.WatchError:
                    continue
    except (redis.RedisError, OSError) as exc:
        raise RunStateUnavailable(str(exc)) from exc
    raise RunStateUnavailable(f"{kind} run state kept changing under us")


def begin_run(
    kind: RunKind, user_id: int, state: dict[str, Any]
) -> dict[str, Any] | None:
    """Start a run unless a live one exists. Returns the new state, or None.

    Raises RunStateUnavailable when Redis is down — starting a run whose state
    cannot be stored would queue work nothing can ever pick up.
    """

    def fn(current):
        if is_live(current):
            return None, None
        new = {
            **state,
            "started_at": now_utc().isoformat(),
            "finished_at": None,
        }
        return new, new

    return _transact(kind, user_id, fn)


def update_run(
    kind: RunKind,
    user_id: int,
    run_id: str,
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    """Merge ``patch`` into the active run ``run_id``.

    Returns the new state, or None when the run was cancelled, finished or
    replaced by a newer one — the caller must stop. An empty patch is a heartbeat.
    """

    def fn(current):
        if not is_active(current) or current["run_id"] != run_id:  # type: ignore[index]
            return None, None
        new = {**current, **patch}  # type: ignore[dict-item]
        return new, new

    return _transact(kind, user_id, fn)


def finish_run(
    kind: RunKind,
    user_id: int,
    run_id: str,
    *,
    patch: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    """Mark the active run ``run_id`` finished. None if it no longer is."""
    return update_run(
        kind,
        user_id,
        run_id,
        {**(patch or {}), "finished_at": now_utc().isoformat(), "error": error},
    )


def cancel_run(kind: RunKind, user_id: int) -> bool:
    """Stop an active run; the running task notices on its next write."""

    def fn(current):
        if not is_active(current):
            return None, False
        new = {
            **current,  # type: ignore[dict-item]
            "remaining": [],
            "cancelled": True,
            "finished_at": now_utc().isoformat(),
        }
        return new, True

    return _transact(kind, user_id, fn)
