"""Worker and broker health for the processing-queue status strip.

Everything here degrades to ``None`` instead of raising: the queue popover must
keep working when Redis or the workers are down — that is when it is most needed.
"""

from __future__ import annotations

import logging
import time

import redis

from app.config import REDIS_URL
from app.services.model_gate import gate_snapshot

logger = logging.getLogger(__name__)

# Celery inspect blocks for its timeout, and the queue popover polls every few
# seconds, so worker liveness is remembered for a while.
_WORKER_CACHE_SECONDS = 30.0
# Node names are `<queue>@<host>` (see the Makefile / compose worker commands).
WORKER_QUEUES = ("ingest", "ai", "maintenance")

_worker_cache: tuple[float, list[dict] | None] | None = None


def _inspect_workers() -> list[dict] | None:
    from app.tasks.celery_app import celery_app

    try:
        stats = celery_app.control.inspect(timeout=1.0).stats()
    except Exception as exc:  # broker down, kombu errors, socket errors
        logger.warning("system_status: worker inspect failed (%s)", exc)
        return None
    stats = stats or {}
    workers = []
    for queue in WORKER_QUEUES:
        nodes = [n for n in stats if n.startswith(f"{queue}@")]
        concurrency = sum(
            (stats[n].get("pool") or {}).get("max-concurrency") or 0 for n in nodes
        )
        workers.append({"name": queue, "up": bool(nodes), "concurrency": concurrency})
    return workers


def worker_status() -> list[dict] | None:
    """Liveness of the ingest/ai/maintenance workers, cached for 30 s."""
    global _worker_cache
    now = time.monotonic()
    if _worker_cache and now - _worker_cache[0] < _WORKER_CACHE_SECONDS:
        return _worker_cache[1]
    result = _inspect_workers()
    _worker_cache = (now, result)
    return result


def queue_depth() -> dict[str, int] | None:
    """Tasks waiting in each broker queue (Redis list length)."""
    try:
        client = redis.Redis.from_url(
            REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5
        )
        try:
            return {name: int(client.llen(name)) for name in WORKER_QUEUES}
        finally:
            client.close()
    except (redis.RedisError, OSError) as exc:
        logger.warning("system_status: queue depth unavailable (%s)", exc)
        return None


_SNAPSHOT_CACHE_SECONDS = 3.0
_snapshot_cache: tuple[float, dict] | None = None


def system_snapshot() -> dict:
    """Workers, backlog and GPU gate; reused for a few seconds across polls."""
    global _snapshot_cache
    now = time.monotonic()
    if _snapshot_cache and now - _snapshot_cache[0] < _SNAPSHOT_CACHE_SECONDS:
        return _snapshot_cache[1]
    snapshot = _build_snapshot()
    _snapshot_cache = (now, snapshot)
    return snapshot


def _build_snapshot() -> dict:
    depth = queue_depth()
    return {
        # Without the broker there is nobody to ask, and Celery's inspect would
        # sit in connection retries; report "unknown" straight away instead.
        "workers": worker_status() if depth is not None else None,
        "queue_depth": depth,
        "gpu_gate": gate_snapshot(),
    }
