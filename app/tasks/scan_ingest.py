import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.tasks.scan_ingest.scan_folder_tick_task",
    max_retries=0,
)
def scan_folder_tick_task(self):
    """Poll the scan incoming folder and ingest any ready .pdf files."""
    from app.config import SessionLocal
    from app.services.ingestion.scan_folder import scan_and_ingest

    db = SessionLocal()
    try:
        count = scan_and_ingest(db)
        if count:
            logger.info("scan_folder_tick: ingested %d file(s)", count)
        return {"status": "ok", "ingested": count}
    except Exception as exc:
        logger.error("scan_folder_tick failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()
