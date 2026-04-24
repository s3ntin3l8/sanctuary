import logging

from app.models.enums import PipelineStage, StageStatus
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.detect_relationships.detect_relationships_task")
def detect_relationships_task(doc_id: int):
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

    logger.info("Doc #%d: relationships started", doc_id)

    # Gate: skip if ENRICH failed or did not produce ai_summary.
    # RELATIONSHIPS uses doc.ai_summary and doc.key_passages — both are only written by ENRICH.
    db = get_db_session()
    try:
        from app.models.database import Document

        doc = db.query(Document).filter(Document.id == doc_id).first()
        stages = (doc.pipeline_stages or {}) if doc else {}
        enrich_status = stages.get(PipelineStage.ENRICH.value, {}).get("status")
        if enrich_status != StageStatus.COMPLETED.value:
            mark_skipped(
                doc_id, PipelineStage.RELATIONSHIPS, db, reason="enrich_not_completed"
            )
            from app.tasks.extract_claims import extract_claims_task

            logger.info(
                "Doc #%d: relationships skipped (enrich_not_completed) — still dispatching claims",
                doc_id,
            )
            extract_claims_task.delay(doc_id)
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "enrich_not_completed",
            }
        if not doc.ai_summary_created_at:
            mark_skipped(
                doc_id, PipelineStage.RELATIONSHIPS, db, reason="missing_ai_summary"
            )
            from app.tasks.extract_claims import extract_claims_task

            logger.info(
                "Doc #%d: relationships skipped (missing_ai_summary) — still dispatching claims",
                doc_id,
            )
            extract_claims_task.delay(doc_id)
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "missing_ai_summary",
            }
    finally:
        db.close()

    try:
        skipped = detect(doc_id)
    except Exception as e:
        logger.error(
            f"Doc {doc_id} relationship detection task failed: {e}", exc_info=True
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.RELATIONSHIPS, db, error=str(e))
        finally:
            db.close()
        from app.tasks.extract_claims import extract_claims_task

        logger.info(
            "Doc #%d: relationships failed — still dispatching claims",
            doc_id,
        )
        extract_claims_task.delay(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db = get_db_session()
    try:
        if skipped:
            mark_skipped(doc_id, PipelineStage.RELATIONSHIPS, db, reason=skipped)
        else:
            mark_completed(doc_id, PipelineStage.RELATIONSHIPS, db)
    finally:
        db.close()

    logger.info(
        "Doc #%d: relationships %s — dispatching claims",
        doc_id,
        "skipped" if skipped else "complete",
    )
    from app.tasks.extract_claims import extract_claims_task

    extract_claims_task.delay(doc_id)
    return {"status": "success", "doc_id": doc_id}
