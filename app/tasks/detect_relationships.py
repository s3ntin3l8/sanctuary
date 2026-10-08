import logging

import httpx
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError as SA_OperationalError

from app.models.enums import PipelineStage, StageStatus
from app.services.pipeline_status import is_db_locked, stages_dict
from app.tasks.ai_retry import retry_if_transient_ai_error
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.detect_relationships.detect_relationships_task",
)
def detect_relationships_task(self, doc_id: int, backfill: bool = True):
    """Detect AI relationships from this doc to prior docs in the same case.

    With ``backfill`` (the default) a successful run also re-queues detection for
    the newer docs that ran before this one existed (see ``relationship_backfill``).
    Those re-runs pass ``backfill=False`` so the pass never cascades.
    """
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
                "Doc #%d: relationships skipped (enrich_not_completed)",
                doc_id,
            )
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
                "Doc #%d: relationships skipped (missing_ai_summary)",
                doc_id,
            )
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "missing_ai_summary",
            }
    finally:
        db.close()

    try:
        skipped = detect(doc_id)
    except httpx.ReadTimeout as e:
        if self.request.retries < 1:
            logger.info("Doc #%d: relationships timeout — retrying once in 90s", doc_id)
            db = get_db_session()
            try:
                from app.services.pipeline_status import schedule_retry

                schedule_retry(
                    doc_id,
                    PipelineStage.RELATIONSHIPS,
                    db,
                    error=f"timeout: {e}",
                    attempt=self.request.retries + 1,
                    max_attempts=1,
                    countdown=90,
                )
            finally:
                db.close()
            raise self.retry(exc=e, countdown=90, max_retries=1) from e
        logger.warning(
            "Doc #%d: relationships timeout after retry (%s) — marking failed",
            doc_id,
            e,
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.RELATIONSHIPS, db, error=f"timeout: {e}")
        finally:
            db.close()
        logger.info(
            "Doc #%d: relationships failed",
            doc_id,
        )
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
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
            "Doc #%d: relationships failed",
            doc_id,
        )
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        if self.request.retries < self.max_retries:
            countdown = 30 * (self.request.retries + 1)
            logger.warning(
                "Doc #%d: AI backend unreachable (%s) — retry %d in %ds",
                doc_id,
                e,
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
        logger.error("Doc #%d: AI backend unreachable after all retries: %s", doc_id, e)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.RELATIONSHIPS, db, error=str(e))
        finally:
            db.close()
        logger.info(
            "Doc #%d: relationships failed",
            doc_id,
        )
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except SoftTimeLimitExceeded as e:
        # An Exception subclass — must come before the generic branch below
        # or it would be treated as a retryable system error, racing
        # self.retry()'s countdown against the imminent hard kill.
        logger.error(f"Doc {doc_id} relationships soft time limit exceeded: {e}")
        db = get_db_session()
        try:
            mark_failed(
                doc_id,
                PipelineStage.RELATIONSHIPS,
                db,
                error=f"soft time limit exceeded: {e}",
            )
        finally:
            db.close()
        logger.info(
            "Doc #%d: relationships failed (soft time limit)",
            doc_id,
        )
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except Exception as e:
        retry_if_transient_ai_error(self, doc_id, PipelineStage.RELATIONSHIPS, e)
        logger.error(
            f"Doc {doc_id} relationship detection task failed: {e}", exc_info=True
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.RELATIONSHIPS, db, error=str(e))
        finally:
            db.close()
        logger.info(
            "Doc #%d: relationships failed",
            doc_id,
        )
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

    if backfill and not skipped:
        try:
            from app.services.intelligence.relationship_backfill import (
                dispatch_backfill,
            )

            dispatch_backfill(doc_id)
        except Exception:
            # Best-effort: a backfill problem must not fail this doc's own stage.
            logger.warning(
                "Doc #%d: relationship backfill failed", doc_id, exc_info=True
            )

    logger.info(
        "Doc #%d: relationships %s",
        doc_id,
        "skipped" if skipped else "complete",
    )
    return {"status": "success", "doc_id": doc_id}
