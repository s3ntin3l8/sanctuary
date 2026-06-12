"""Live Celery worker pool control — resize the `ai` worker without a restart.

Uses Celery's remote-control API (the same pidbox bus already used by
settings_maintenance's `control.purge()`). `--without-gossip/mingle/heartbeat`
do not disable remote control, so this works against the trimmed workers.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Worker node-name prefix for the ai queue (compose runs `-n ai@%h`).
_AI_NODE_PREFIX = "ai@"


def apply_ai_concurrency(target: int) -> dict:
    """Resize every live `ai@` worker's prefork pool to `target` processes.

    Returns {"live": bool, "nodes": [{"node", "from", "to"}, ...]}.
    `live` is False when no ai worker answered the control ping (stats() is
    None/empty) — the caller persists the value regardless; it applies at the
    worker's next boot via the entrypoint.
    """
    from app.tasks.celery_app import celery_app

    insp = celery_app.control.inspect(timeout=1.5)
    stats = insp.stats() if insp is not None else None
    if not stats:
        return {"live": False, "nodes": []}

    applied: list[dict] = []
    for node, node_stats in stats.items():
        if not node.startswith(_AI_NODE_PREFIX):
            continue
        current = (node_stats.get("pool") or {}).get("max-concurrency")
        if not isinstance(current, int):
            logger.warning("worker_control: no max-concurrency for %s", node)
            continue
        delta = target - current
        if delta > 0:
            celery_app.control.pool_grow(delta, destination=[node])
        elif delta < 0:
            celery_app.control.pool_shrink(-delta, destination=[node])
        applied.append({"node": node, "from": current, "to": target})
        logger.info("worker_control: resized %s pool %d -> %d", node, current, target)

    return {"live": bool(applied), "nodes": applied}
