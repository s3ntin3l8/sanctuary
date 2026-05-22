import logging

import httpx
from sqlalchemy.exc import OperationalError as SA_OperationalError

from app.models.enums import PipelineStage
from app.services.pipeline_status import is_db_locked, stages_dict
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _trigger_case_brief(doc_id: int) -> None:
    from app.config import SessionLocal
    from app.models.database import Case, Document
    from app.tasks.generate_case_brief import generate_case_brief_task

    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc or not doc.case_id or doc.case_id == "_TRIAGE":
            return
        case_id = doc.case_id

        # DB guard: skip dispatch if a brief is already actively running.
        # generate_case_brief_task calls _mark_processing() which sets
        # ai_brief={"status":"processing"} and commits before the AI call
        # starts — so this is the authoritative signal that covers the full
        # task lifetime, unlike a short-TTL Redis key.
        case = db.query(Case).filter(Case.id == case_id).first()
        if (
            case
            and isinstance(case.ai_brief, dict)
            and case.ai_brief.get("status") == "processing"
        ):
            logger.debug(
                "Case %s brief already in progress — skipping dispatch", case_id
            )
            return
    except Exception as e:
        logger.warning("Could not trigger case brief for doc %d: %s", doc_id, e)
        return
    finally:
        db.close()

    # Redis NX lock: second guard that collapses near-simultaneous dispatches
    # (e.g. N docs in the same case all completing CLAIMS at once) before the
    # first task has had time to set ai_brief=processing.  The 30 s TTL covers
    # the window between dispatch and _mark_processing().
    # Falls back to always-dispatch when Redis is unavailable (logged, not silent).
    _DEDUP_TTL = 30
    lock_key = f"sanctuary:case_brief_pending:{case_id}"
    try:
        from app.services.ai_inflight import _get_sync_client

        if not _get_sync_client().set(lock_key, "1", nx=True, ex=_DEDUP_TTL):
            logger.debug(
                "Case %s brief already queued — skipping duplicate dispatch", case_id
            )
            return
    except Exception as exc:
        logger.warning(
            "Redis dedup check failed for case %s, dispatching anyway: %s",
            case_id,
            exc,
        )

    try:
        generate_case_brief_task.delay(case_id)
    except Exception as e:
        logger.warning("Could not trigger case brief for doc %d: %s", doc_id, e)


@celery_app.task(
    bind=True, max_retries=3, name="app.tasks.extract_claims.extract_claims_task"
)
def extract_claims_task(self, doc_id: int):
    """Extract factual/legal/procedural claims from a document and link evidence to existing claims."""
    from app.dependencies import get_db_session
    from app.models.database import Document
    from app.models.enums import StageStatus
    from app.services.intelligence.claim_extractor import extract
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_skipped,
        mark_started,
    )

    # Gate: CLAIMS uses doc.ai_summary — must have been written by ENRICH.
    # Check before mark_started so the stage is not recorded as "started" for a skip.
    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        stages = stages_dict(doc) if doc else {}
        enrich_status = stages.get(PipelineStage.ENRICH.value, {}).get("status")
        if enrich_status != StageStatus.COMPLETED.value:
            mark_skipped(
                doc_id, PipelineStage.CLAIMS, db, reason="enrich_not_completed"
            )
            logger.info("Doc #%d: claims skipped (enrich_not_completed)", doc_id)
            _trigger_case_brief(doc_id)
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "enrich_not_completed",
            }
        if not doc.ai_summary_created_at:
            mark_skipped(doc_id, PipelineStage.CLAIMS, db, reason="missing_ai_summary")
            logger.info("Doc #%d: claims skipped (missing_ai_summary)", doc_id)
            _trigger_case_brief(doc_id)
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "missing_ai_summary",
            }
    finally:
        db.close()

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.CLAIMS, db)
    finally:
        db.close()

    logger.info("Doc #%d: claims started", doc_id)
    try:
        skipped = extract(doc_id)
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
                    PipelineStage.CLAIMS,
                    db,
                    error=str(e),
                    attempt=self.request.retries + 1,
                    max_attempts=self.max_retries,
                    countdown=countdown,
                )
            finally:
                db.close()
            raise self.retry(exc=e, countdown=countdown) from e
        logger.error(f"Doc {doc_id} claim extraction task failed: {e}", exc_info=True)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.CLAIMS, db, error=str(e))
        finally:
            db.close()
        logger.info("Doc #%d: claims failed — still triggering case brief", doc_id)
        _trigger_case_brief(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.error("Doc #%d: AI backend unreachable: %s", doc_id, e)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.CLAIMS, db, error=str(e))
        finally:
            db.close()
        logger.info("Doc #%d: claims failed — still triggering case brief", doc_id)
        _trigger_case_brief(doc_id)
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
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
        if skipped:
            mark_skipped(doc_id, PipelineStage.CLAIMS, db, reason=skipped)
        else:
            mark_completed(doc_id, PipelineStage.CLAIMS, db)
            from app.services.ingestion.service import refresh_review_reasons

            doc = db.query(Document).filter(Document.id == doc_id).first()
            if doc:
                refresh_review_reasons(doc, db)
    finally:
        db.close()

    logger.info(
        "Doc #%d: claims %s",
        doc_id,
        f"skipped ({skipped})" if skipped else "complete",
    )
    _trigger_case_brief(doc_id)
    return {"status": "success", "doc_id": doc_id}
