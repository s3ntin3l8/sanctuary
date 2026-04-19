import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, max_retries=3, name="app.tasks.enrich_document.enrich_document_task"
)
def enrich_document_task(self, doc_id: int):
    """Run per-document AI enrichment, then enqueue relationship detection and cost rollup."""
    from app.services.intelligence.document_enricher import enrich

    try:
        enrich(doc_id)
    except Exception as e:
        logger.error(f"Doc {doc_id} enrichment task failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    from app.tasks.detect_relationships import detect_relationships_task

    detect_relationships_task.delay(doc_id)

    _trigger_cost_rollup(doc_id)

    return {"status": "success", "doc_id": doc_id}


def _trigger_cost_rollup(doc_id: int) -> None:
    from app.config import SessionLocal
    from app.models.database import Document
    from app.services.case_service import recompute_total_cost_exposure

    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc and doc.case_id and doc.case_id != "_TRIAGE":
            recompute_total_cost_exposure(doc.case_id, db)
    except Exception as e:
        logger.warning(f"Cost rollup failed for doc {doc_id}: {e}")
    finally:
        db.close()
