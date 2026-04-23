import logging

from app.models.enums import PipelineStage
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


@celery_app.task(name="app.tasks.extract_claims.extract_claims_task")
def extract_claims_task(doc_id: int):
    """Extract factual/legal/procedural claims from a document and link evidence to existing claims."""
    from app.dependencies import get_db_session
    from app.services.intelligence.claim_extractor import extract
    from app.services.pipeline_status import mark_completed, mark_failed, mark_started

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.CLAIMS, db)
    finally:
        db.close()

    logger.info("Doc #%d: claims started", doc_id)
    try:
        extract(doc_id)
    except Exception as e:
        logger.error(f"Doc {doc_id} claim extraction task failed: {e}", exc_info=True)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.CLAIMS, db, error=str(e))
        finally:
            db.close()
        logger.info("Doc #%d: claims failed — still triggering case brief", doc_id)
        _trigger_case_brief(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db = get_db_session()
    try:
        mark_completed(doc_id, PipelineStage.CLAIMS, db)
    finally:
        db.close()

    logger.info("Doc #%d: claims complete", doc_id)
    _trigger_case_brief(doc_id)
    return {"status": "success", "doc_id": doc_id}
