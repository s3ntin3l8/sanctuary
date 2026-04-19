import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _trigger_case_brief(doc_id: int) -> None:
    from app.config import SessionLocal
    from app.models.database import Document
    from app.tasks.generate_case_brief import generate_case_brief_task

    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc and doc.case_id and doc.case_id != "_TRIAGE":
            generate_case_brief_task.delay(doc.case_id)
    except Exception as e:
        logger.warning(f"Could not trigger case brief for doc {doc_id}: {e}")
    finally:
        db.close()


@celery_app.task(
    bind=True, max_retries=3, name="app.tasks.extract_claims.extract_claims_task"
)
def extract_claims_task(self, doc_id: int):
    """Extract factual/legal/procedural claims from a document and link evidence to existing claims."""
    from app.services.intelligence.claim_extractor import extract

    try:
        extract(doc_id)
        _trigger_case_brief(doc_id)
        return {"status": "success", "doc_id": doc_id}
    except Exception as e:
        logger.error(f"Doc {doc_id} claim extraction task failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
