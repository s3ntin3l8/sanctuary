import logging

import httpx
from sqlalchemy.exc import OperationalError as SA_OperationalError

from app.models.enums import PipelineStage, StageStatus
from app.services.pipeline_status import is_db_locked, stages_dict
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _dispatch_claims_safely(doc_id: int) -> None:
    """Dispatch extract_claims_task; on broker failure mark CLAIMS failed instead
    of leaving the stage stuck in PENDING."""
    from app.dependencies import get_db_session
    from app.services.pipeline_status import mark_failed
    from app.tasks.extract_claims import extract_claims_task

    try:
        extract_claims_task.delay(doc_id)
    except Exception as e:
        logger.error(
            "Doc #%d: extract_claims dispatch failed — marking CLAIMS failed: %s",
            doc_id,
            e,
            exc_info=True,
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.CLAIMS, db, error=f"dispatch failed: {e}")
        finally:
            db.close()


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

    logger.info("Doc #%d: relationships started", doc_id)

    # Gate: skip if ENRICH failed or did not produce ai_summary.
    # RELATIONSHIPS uses doc.ai_summary and doc.key_passages — both are only written by ENRICH.
    db = get_db_session()
    try:
        from app.models.database import Document

        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc is None:
            logger.warning(
                "Doc #%d: not found — skipping relationship detection", doc_id
            )
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "document_not_found",
            }
        stages = stages_dict(doc)
        enrich_status = stages.get(PipelineStage.ENRICH.value, {}).get("status")
        if enrich_status != StageStatus.COMPLETED.value:
            mark_skipped(
                doc_id, PipelineStage.RELATIONSHIPS, db, reason="enrich_not_completed"
            )
            logger.info(
                "Doc #%d: relationships skipped (enrich_not_completed) — still dispatching claims",
                doc_id,
            )
            _dispatch_claims_safely(doc_id)
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "enrich_not_completed",
            }
        if not doc.ai_summary_created_at:
            mark_skipped(
                doc_id, PipelineStage.RELATIONSHIPS, db, reason="missing_ai_summary"
            )
            logger.info(
                "Doc #%d: relationships skipped (missing_ai_summary) — still dispatching claims",
                doc_id,
            )
            _dispatch_claims_safely(doc_id)
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "missing_ai_summary",
            }
    finally:
        db.close()

    try:
        skipped = detect(doc_id)
    except SA_OperationalError as e:
        if is_db_locked(e) and self.request.retries < self.max_retries:
            countdown = 30 * (self.request.retries + 1)
            logger.warning(
                "Doc #%d: db locked — retry %d in %ds",
                doc_id,
                self.request.retries + 1,
                countdown,
            )
            db = get_db_session()
            try:
                from app.services.pipeline_status import schedule_retry

                schedule_retry(
                    doc_id,
                    PipelineStage.RELATIONSHIPS,
                    db,
                    error=str(e),
                    attempt=self.request.retries + 1,
                    max_attempts=self.max_retries,
                    countdown=countdown,
                )
            finally:
                db.close()
            raise self.retry(exc=e, countdown=countdown) from e
        logger.error(
            f"Doc {doc_id} relationship detection task failed: {e}", exc_info=True
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.RELATIONSHIPS, db, error=str(e))
        finally:
            db.close()
        logger.info(
            "Doc #%d: relationships failed — still dispatching claims",
            doc_id,
        )
        _dispatch_claims_safely(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.error("Doc #%d: AI backend unreachable: %s", doc_id, e)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.RELATIONSHIPS, db, error=str(e))
        finally:
            db.close()
        logger.info(
            "Doc #%d: relationships failed — still dispatching claims",
            doc_id,
        )
        _dispatch_claims_safely(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except Exception as e:
        logger.error(
            f"Doc {doc_id} relationship detection task failed: {e}", exc_info=True
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.RELATIONSHIPS, db, error=str(e))
        finally:
            db.close()
        logger.info(
            "Doc #%d: relationships failed — still dispatching claims",
            doc_id,
        )
        _dispatch_claims_safely(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db = get_db_session()
    try:
        if skipped:
            mark_skipped(doc_id, PipelineStage.RELATIONSHIPS, db, reason=skipped)
        else:
            mark_completed(doc_id, PipelineStage.RELATIONSHIPS, db)
            from app.models.database import Document
            from app.services.ingestion.service import refresh_review_reasons

            doc = db.query(Document).filter(Document.id == doc_id).first()
            if doc:
                refresh_review_reasons(doc, db)
    finally:
        db.close()

    logger.info(
        "Doc #%d: relationships %s — dispatching claims",
        doc_id,
        "skipped" if skipped else "complete",
    )
    _dispatch_claims_safely(doc_id)
    return {"status": "success", "doc_id": doc_id}
