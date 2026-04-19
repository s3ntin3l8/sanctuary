import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=2,
    name="app.tasks.prepare_slicing.prepare_slicing_task",
)
def prepare_slicing_task(self, batch_id: int):
    """Render thumbnails, OCR, and propose slicing cuts for a multi-page scan batch."""
    from app.services.ingestion.slicer import prepare

    try:
        prepare(batch_id)
        return {"status": "success", "batch_id": batch_id}
    except Exception as exc:
        logger.error("prepare_slicing_task batch %d failed: %s", batch_id, exc)
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc, countdown=30 * (self.request.retries + 1)
            ) from exc
        return {"status": "failed", "batch_id": batch_id, "error": str(exc)}
