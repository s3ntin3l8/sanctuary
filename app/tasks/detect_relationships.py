import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.detect_relationships.detect_relationships_task",
)
def detect_relationships_task(self, doc_id: int):
    """Detect AI relationships from this doc to prior docs in the same proceeding."""
    from app.services.intelligence.relationship_detector import detect

    try:
        detect(doc_id)
    except Exception as e:
        logger.error(f"Doc {doc_id} relationship detection task failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    from app.tasks.extract_claims import extract_claims_task

    extract_claims_task.delay(doc_id)
    return {"status": "success", "doc_id": doc_id}
