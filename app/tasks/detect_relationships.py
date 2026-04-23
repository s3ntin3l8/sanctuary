import logging

from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.detect_relationships.detect_relationships_task",
)
def detect_relationships_task(self, doc_id: int):
    """Detect AI relationships from this doc to prior docs in the same proceeding."""
    from app.dependencies import get_db_session
    from app.services.intelligence.relationship_detector import detect
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_skipped,
        mark_started,
    )

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.RELATIONSHIPS, db)
    finally:
        db.close()

    try:
        skipped = detect(doc_id)
    except Exception as e:
        logger.error(f"Doc {doc_id} relationship detection task failed: {e}")
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.RELATIONSHIPS, db, error=str(e))
        finally:
            db.close()
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db = get_db_session()
    try:
        if skipped:
            mark_skipped(doc_id, PipelineStage.RELATIONSHIPS, db, reason=skipped)
        else:
            mark_completed(doc_id, PipelineStage.RELATIONSHIPS, db)
    finally:
        db.close()

    from app.tasks.extract_claims import extract_claims_task

    extract_claims_task.delay(doc_id)
    return {"status": "success", "doc_id": doc_id}
