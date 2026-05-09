import logging

import httpx

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
        schedule_retry,
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
    except httpx.ReadTimeout as e:
        if self.request.retries < 1:
            logger.info("Doc #%d: enrich timeout — retrying once in 90s", doc_id)
            db = get_db_session()
            try:
                schedule_retry(
                    doc_id,
                    PipelineStage.ENRICH,
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
            "Doc #%d: enrich timeout after retry (%s) — marking failed", doc_id, e
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.ENRICH, db, error=f"timeout: {e}")
        finally:
            db.close()
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except httpx.ConnectError as e:
        # Backend not yet reachable (startup race, proxy restarting, etc.)
        # Retry quickly without a noisy ERROR traceback.
        if self.request.retries < self.max_retries:
            countdown = 15
            logger.warning(
                "Doc #%d: AI backend unreachable (%s) — retry %d in %ds",
                doc_id,
                e,
                self.request.retries + 1,
                countdown,
            )
            db = get_db_session()
            try:
                schedule_retry(
                    doc_id,
                    PipelineStage.ENRICH,
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
            mark_failed(doc_id, PipelineStage.ENRICH, db, error=f"connect: {e}")
        finally:
            db.close()
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except Exception as e:
        logger.error(f"Doc {doc_id} enrichment task failed: {e}", exc_info=True)
        if self.request.retries < self.max_retries:
            countdown = 60 * (self.request.retries + 1)
            db = get_db_session()
            try:
                schedule_retry(
                    doc_id,
                    PipelineStage.ENRICH,
                    db,
                    error=str(e),
                    attempt=self.request.retries + 1,
                    max_attempts=self.max_retries,
                    countdown=countdown,
                )
            finally:
                db.close()
            raise self.retry(exc=e, countdown=countdown) from e

        # All retries exhausted — terminal failure.
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.ENRICH, db, error=str(e))
        finally:
            db.close()
        logger.info(
            "Doc #%d: enrich failed permanently — still dispatching relationships",
            doc_id,
        )
        _dispatch_safely(doc_id, PipelineStage.RELATIONSHIPS)
        _dispatch_safely(doc_id, PipelineStage.ENTITIES)
        _trigger_cost_rollup(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db = get_db_session()
    try:
        mark_completed(doc_id, PipelineStage.ENRICH, db)
    finally:
        db.close()

    logger.info("Doc #%d: enrich complete — dispatching relationships", doc_id)

    _dispatch_if_pending(doc_id, PipelineStage.RELATIONSHIPS)
    _dispatch_if_pending(doc_id, PipelineStage.ENTITIES)

    _trigger_cost_rollup(doc_id)

    return {"status": "success", "doc_id": doc_id}


def _task_for_stage(stage: PipelineStage):
    """Map a pipeline stage to the Celery task that runs it."""
    if stage == PipelineStage.RELATIONSHIPS:
        from app.tasks.detect_relationships import detect_relationships_task

        return detect_relationships_task
    if stage == PipelineStage.ENTITIES:
        from app.tasks.extract_entities import extract_entities_task

        return extract_entities_task
    raise ValueError(f"No task mapping for stage {stage}")


def _dispatch_safely(doc_id: int, stage: PipelineStage) -> None:
    """Dispatch the task for `stage`; on broker failure mark the stage failed
    instead of leaving it stuck in PENDING."""
    from app.dependencies import get_db_session
    from app.services.pipeline_status import mark_failed

    task = _task_for_stage(stage)
    try:
        task.delay(doc_id)
    except Exception as e:
        logger.error(
            "Doc #%d: dispatch of %s failed — marking stage failed: %s",
            doc_id,
            stage.value,
            e,
            exc_info=True,
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, stage, db, error=f"dispatch failed: {e}")
        finally:
            db.close()


def _dispatch_if_pending(doc_id: int, stage: PipelineStage) -> None:
    """Atomically claim a pending stage and fire its task — prevents fan-out from
    concurrent enrich retries. Dispatch failures mark the stage failed."""
    from app.dependencies import get_db_session
    from app.services.pipeline_status import claim_stage_for_dispatch

    db = get_db_session()
    try:
        claimed = claim_stage_for_dispatch(doc_id, stage, db)
    finally:
        db.close()
    if claimed:
        _dispatch_safely(doc_id, stage)


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
