import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, max_retries=3, name="app.tasks.extract_claims.extract_claims_task"
)
def extract_claims_task(self, doc_id: int):
    """Extract factual/legal/procedural claims from a document and link evidence to existing claims."""
    from app.services.intelligence.claim_extractor import extract

    try:
        extract(doc_id)
        return {"status": "success", "doc_id": doc_id}
    except Exception as e:
        logger.error(f"Doc {doc_id} claim extraction task failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
