import logging

from app.config import SessionLocal
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.thread_open_scan.thread_open_scan_task")
def thread_open_scan_task():
    """Periodic scan: close threads whose reply relationship has arrived."""
    from app.services.intelligence.thread_open_scanner import scan_and_close_threads

    db = SessionLocal()
    try:
        updated = scan_and_close_threads(db)
        return {"status": "success", "closed": updated}
    except Exception as e:
        logger.error(f"Thread-open scan failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()
