import logging

import httpx
from sqlalchemy.exc import OperationalError as SA_OperationalError

from app.models.enums import PipelineStage
from app.services.pipeline_status import is_db_locked, stages_dict
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, max_retries=3, name="app.tasks.extract_entities.extract_entities_task"
)
def extract_entities_task(self, doc_id: int):
    """Extract named entities (persons, courts, law firms, citations) from a document."""
    from app.dependencies import get_db_session
    from app.models.database import Document
    from app.models.enums import StageStatus
    from app.services.intelligence.entity_extractor import extract
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_skipped,
        mark_started,
    )

    # Gate: ENTITIES uses doc.ai_summary — must have been written by ENRICH.
    # Check before mark_started so the stage is not recorded as "started" for a skip.
    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        stages = stages_dict(doc) if doc else {}
        enrich_status = stages.get(PipelineStage.ENRICH.value, {}).get("status")
        if enrich_status != StageStatus.COMPLETED.value:
            mark_skipped(
                doc_id, PipelineStage.ENTITIES, db, reason="enrich_not_completed"
            )
            logger.info("Doc #%d: entities skipped (enrich_not_completed)", doc_id)
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "enrich_not_completed",
            }
        if not doc.ai_summary_created_at:
            mark_skipped(
                doc_id, PipelineStage.ENTITIES, db, reason="missing_ai_summary"
            )
            logger.info("Doc #%d: entities skipped (missing_ai_summary)", doc_id)
            return {
                "status": "skipped",
                "doc_id": doc_id,
                "reason": "missing_ai_summary",
            }
    finally:
        db.close()

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.ENTITIES, db)
    finally:
        db.close()

    logger.info("Doc #%d: entities started", doc_id)
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
                    PipelineStage.ENTITIES,
                    db,
                    error=str(e),
                    attempt=self.request.retries + 1,
                    max_attempts=self.max_retries,
                    countdown=countdown,
                )
            finally:
                db.close()
            raise self.retry(exc=e, countdown=countdown) from e
        logger.error(f"Doc {doc_id} entity extraction task failed: {e}", exc_info=True)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.ENTITIES, db, error=str(e))
        finally:
            db.close()
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        logger.error("Doc #%d: AI backend unreachable: %s", doc_id, e)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.ENTITIES, db, error=str(e))
        finally:
            db.close()
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    except Exception as e:
        logger.error(f"Doc {doc_id} entity extraction task failed: {e}", exc_info=True)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.ENTITIES, db, error=str(e))
        finally:
            db.close()
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db = get_db_session()
    try:
        if skipped:
            mark_skipped(doc_id, PipelineStage.ENTITIES, db, reason=skipped)
        else:
            mark_completed(doc_id, PipelineStage.ENTITIES, db)
    finally:
        db.close()

    logger.info(
        "Doc #%d: entities %s",
        doc_id,
        f"skipped ({skipped})" if skipped else "complete",
    )
    return {"status": "success", "doc_id": doc_id}
