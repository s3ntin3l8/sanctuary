import logging

import httpx

from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, max_retries=1, name="app.tasks.enrich_document.enrich_document_task"
)
def enrich_document_task(self, doc_id: int):
    """Run per-document AI enrichment, then enqueue relationship detection and cost rollup."""
    from app.dependencies import get_db_session
    from app.models.database import Document
    from app.models.enums import StageStatus
    from app.services.intelligence.document_enricher import enrich
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_skipped,
        mark_started,
        schedule_retry,
        stages_dict,
    )

    _TERMINAL = {
        StageStatus.COMPLETED.value,
        StageStatus.FAILED.value,
        StageStatus.SKIPPED.value,
    }

    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc:
            stages = stages_dict(doc)
            # Primary gate: BATCH_ANALYSIS must be terminal before ENRICH runs.
            # The enricher uses doc.role / doc.attributed_originator set by
            # batch analysis for cover-letter framing. An ENRICH that runs
            # early with stale STANDALONE context produces wrong output and
            # triggers the cascade-reset in analyze_batch.py:196 — which
            # cascades to CLAIMS/RELATIONSHIPS/ENTITIES and creates the
            # "clearing N stale claims" / "auto-merged duplicate" log storm.
            #
            # Skipping here is correct: when analyze_batch_task finishes it
            # calls _enrich_if_pending(doc_id) for every doc, which re-fires
            # this task with batch_analysis now terminal.
            #
            # Docs without a batch (manual uploads, ingest_batch_id IS NULL)
            # bypass batch_analysis entirely — their batch_analysis stage row
            # is SKIPPED at initialize-time, so this check passes immediately.
            batch_analysis_status = stages.get("batch_analysis", {}).get("status")
            if batch_analysis_status not in _TERMINAL:
                mark_skipped(
                    doc_id,
                    PipelineStage.ENRICH,
                    db,
                    reason="batch_analysis_not_completed",
                )
                logger.info(
                    "Doc #%d: skipping enrich — batch_analysis not yet terminal "
                    "(status=%s); analyze_batch_task will redispatch",
                    doc_id,
                    batch_analysis_status,
                )
                return {
                    "status": "skipped",
                    "doc_id": doc_id,
                    "reason": "batch_analysis_not_completed",
                }

            # Secondary gate: skip enrichment when METADATA failed.
            # This prevents batch-dispatched enrichment from running against docs
            # that have no sender/tier/summary (pipeline would fly blind).
            metadata_status = stages.get("metadata", {}).get("status")
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
        # Retry quietly without a noisy ERROR traceback.
        if self.request.retries < self.max_retries:
            from app.services.ai_provider import chat_provider

            countdown = 30
            logger.warning(
                "Doc #%d: AI backend unreachable at %s (%s) — retry %d in %ds",
                doc_id,
                chat_provider.base_url,
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
        from app.services.ai_provider import chat_provider

        logger.error(
            "Doc #%d: AI backend unreachable at %s after all retries: %s",
            doc_id,
            chat_provider.base_url,
            e,
        )
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.ENRICH, db, error=f"connect: {e}")
        finally:
            db.close()
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except httpx.HTTPStatusError as e:
        # 4xx = client-side error — the exact same request will always fail.
        # Fail immediately without burning retries; 5xx may be transient so
        # fall through to the generic handler below.
        if 400 <= e.response.status_code < 500:
            logger.error(
                "Doc #%d: AI returned HTTP %d — failing immediately (no retry): %s",
                doc_id,
                e.response.status_code,
                e,
            )
            db = get_db_session()
            try:
                mark_failed(
                    doc_id,
                    PipelineStage.ENRICH,
                    db,
                    error=f"HTTP {e.response.status_code}: {e}",
                )
            finally:
                db.close()
            return {"status": "failed", "doc_id": doc_id, "error": str(e)}
        # 5xx — may be transient; let the generic handler below decide on retry.
        logger.error(
            "Doc #%d: AI returned HTTP %d — treating as transient: %s",
            doc_id,
            e.response.status_code,
            e,
        )
        raise RuntimeError(str(e)) from e
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
