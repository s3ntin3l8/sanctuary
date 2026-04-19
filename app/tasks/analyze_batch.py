import logging

from app.config import SessionLocal
from app.models.database import Document
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, max_retries=3, name="app.tasks.analyze_batch.analyze_batch_task"
)
def analyze_batch_task(self, batch_id: int):
    """Run batch-level AI analysis (cover-letter detection + action items), then enqueue per-doc enrichment."""
    from app.services.intelligence.batch_analyzer import analyze

    try:
        analyze(batch_id)
    except Exception as e:
        logger.error(f"Batch {batch_id} analysis failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "batch_id": batch_id, "error": str(e)}

    db = SessionLocal()
    try:
        doc_ids = [
            r[0]
            for r in db.query(Document.id)
            .filter(Document.ingest_batch_id == batch_id)
            .all()
        ]
    finally:
        db.close()

    for doc_id in doc_ids:
        enrich_document_task.delay(doc_id)

    return {"status": "success", "batch_id": batch_id, "enqueued_docs": len(doc_ids)}


from app.tasks.enrich_document import (
    enrich_document_task,  # noqa: E402 — avoids circular at task-registration time
)
