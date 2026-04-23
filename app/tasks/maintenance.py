import logging
from datetime import UTC, datetime, timedelta

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

_MAX_AGE_DAYS = 30


@celery_app.task(name="app.tasks.maintenance.prune_ai_debug_logs_task")
def prune_ai_debug_logs_task():
    """Delete ai_debug log files older than 30 days."""
    from app.config import DATA_DIR

    debug_dir = DATA_DIR / "ai_debug"
    if not debug_dir.exists():
        return {"status": "skipped", "reason": "ai_debug directory does not exist"}

    cutoff = datetime.now(UTC) - timedelta(days=_MAX_AGE_DAYS)
    cutoff_ts = cutoff.timestamp()

    deleted = 0
    errors = 0
    for log_file in debug_dir.iterdir():
        if not log_file.is_file():
            continue
        try:
            if log_file.stat().st_mtime < cutoff_ts:
                log_file.unlink()
                deleted += 1
        except OSError as exc:
            logger.warning("Could not remove ai_debug file %s: %s", log_file, exc)
            errors += 1

    logger.info(
        "prune_ai_debug_logs: deleted=%d errors=%d cutoff=%s",
        deleted,
        errors,
        cutoff.date().isoformat(),
    )
    return {"status": "success", "deleted": deleted, "errors": errors}
