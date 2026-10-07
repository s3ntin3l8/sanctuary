"""Progress/control state for long-running Gmail jobs (index refresh, import).

One JSON blob per (kind, user) in Redis. The import task is a chain of short
hops that read this state each time, so it is also the control channel:
``cancel_run`` flips it and the next hop stops. Redis being down degrades open
(no state => the page shows nothing running), same convention as the sync lock.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import redis

from app.config import REDIS_URL
from app.core.timezone import now_utc

logger = logging.getLogger(__name__)

RunKind = Literal["index", "import"]

_ACTIVE_TTL_SECONDS = 24 * 3600
_FINISHED_TTL_SECONDS = 3600

_client: redis.Redis | None = None


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


def save_run(kind: RunKind, user_id: int, state: dict[str, Any]) -> None:
    ttl = _FINISHED_TTL_SECONDS if state.get("finished_at") else _ACTIVE_TTL_SECONDS
    try:
        _get_client().set(_key(kind, user_id), json.dumps(state), ex=ttl)
    except (redis.RedisError, OSError):
        logger.warning("gmail %s run state not saved (Redis unavailable)", kind)


def begin_run(
    kind: RunKind, user_id: int, state: dict[str, Any]
) -> dict[str, Any] | None:
    """Start a run unless one is already active. Returns the new state, or None."""
    if is_active(get_run(kind, user_id)):
        return None
    state = {**state, "started_at": now_utc().isoformat(), "finished_at": None}
    save_run(kind, user_id, state)
    return state


def finish_run(
    kind: RunKind, user_id: int, state: dict[str, Any], *, error: str | None = None
) -> None:
    save_run(
        kind,
        user_id,
        {**state, "finished_at": now_utc().isoformat(), "error": error},
    )


def cancel_run(kind: RunKind, user_id: int) -> bool:
    """Stop an active run; the running task notices on its next hop."""
    state = get_run(kind, user_id)
    if not is_active(state):
        return False
    assert state is not None
    finish_run(kind, user_id, {**state, "remaining": [], "cancelled": True})
    return True
