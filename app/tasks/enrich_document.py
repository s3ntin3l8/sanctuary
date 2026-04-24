import logging

from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, max_retries=3, name="app.tasks.enrich_document.enrich_document_task"
)
def enrich_document_task(self, doc_id: int):
    """Run per-document AI enrichment, then enqueue relationship detection and cost rollup."""
    from app.dependencies import get_db_session
    from app.models.database import Document
    from app.services.intelligence.document_enricher import enrich
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_skipped,
        mark_started,
    )

    # Secondary gate: skip enrichment when METADATA failed.
    # This prevents batch-dispatched enrichment from running against docs
    # that have no sender/tier/summary (pipeline would fly blind).
    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc:
            metadata_status = (
                (doc.pipeline_stages or {}).get("metadata", {}).get("status")
            )
            if metadata_status == "failed":
                mark_skipped(doc_id, PipelineStage.ENRICH, db, reason="metadata_failed")
                logger.info("Doc #%d: skipping enrich — METADATA failed", doc_id)
                return {
                    "status": "skipped",
                    "doc_id": doc_id,
                    "reason": "metadata_failed",
                }
    finally:
        db.close()

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.ENRICH, db)
    finally:
        db.close()

    logger.info("Doc #%d: enrich started", doc_id)
    try:
        enrich(doc_id)
    except Exception as e:
        logger.error(f"Doc {doc_id} enrichment task failed: {e}", exc_info=True)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.ENRICH, db, error=str(e))
        finally:
            db.close()
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        from app.tasks.detect_relationships import detect_relationships_task
        from app.tasks.extract_entities import extract_entities_task

        logger.info(
            "Doc #%d: enrich failed permanently — still dispatching relationships",
            doc_id,
        )
        detect_relationships_task.delay(doc_id)
        extract_entities_task.delay(doc_id)
        _trigger_cost_rollup(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db = get_db_session()
    try:
        mark_completed(doc_id, PipelineStage.ENRICH, db)
    finally:
        db.close()

    logger.info("Doc #%d: enrich complete — dispatching relationships", doc_id)
    from app.tasks.detect_relationships import detect_relationships_task
    from app.tasks.extract_entities import extract_entities_task

    detect_relationships_task.delay(doc_id)
    extract_entities_task.delay(doc_id)

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
