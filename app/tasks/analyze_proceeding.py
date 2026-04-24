import logging

from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.analyze_proceeding.analyze_proceeding_task",
)
def analyze_proceeding_task(self, doc_id: int):
    """Analyze document for proceeding changes (escalation, court details), then trigger batch analysis."""
    from app.dependencies import get_db_session
    from app.models.database import Document
    from app.services.ai_config import get_effective_config
    from app.services.intelligence.proceeding_analyzer import (
        analyze_and_update_proceeding,
    )
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_skipped,
        mark_started,
    )

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.PROCEEDING_ANALYSIS, db)
    finally:
        db.close()

    logger.info("Doc #%d: proceeding analysis started", doc_id)

    batch_id = None
    try:
        db = get_db_session()
        try:
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if not doc:
                logger.warning("Doc #%d: not found for proceeding analysis", doc_id)
                return {"status": "not_found", "doc_id": doc_id}

            batch_id = doc.ingest_batch_id
            config = get_effective_config(db)

            skip_reason = analyze_and_update_proceeding(doc, config.summary_model, db)

            if skip_reason:
                mark_skipped(
                    doc_id, PipelineStage.PROCEEDING_ANALYSIS, db, reason=skip_reason
                )
                logger.info(
                    "Doc #%d: proceeding analysis skipped (%s)", doc_id, skip_reason
                )
            else:
                mark_completed(doc_id, PipelineStage.PROCEEDING_ANALYSIS, db)
                logger.info("Doc #%d: proceeding analysis complete", doc_id)
        finally:
            db.close()

    except Exception as e:
        logger.error(f"Doc {doc_id} proceeding analysis failed: {e}", exc_info=True)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.PROCEEDING_ANALYSIS, db, error=str(e))
        finally:
            db.close()

        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e

        # Continue to batch analysis even if failed, if we have a batch_id and all siblings are done
        if batch_id:
            from app.services.intelligence.orchestrator import claim_batch_for_analysis

            db_claim = get_db_session()
            try:
                if claim_batch_for_analysis(batch_id, db_claim):
                    from app.tasks.analyze_batch import analyze_batch_task

                    analyze_batch_task.delay(batch_id)
            finally:
                db_claim.close()

        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    # Trigger next step: Batch analysis (only if all docs in batch are ready)
    if batch_id:
        from app.services.intelligence.orchestrator import claim_batch_for_analysis

        db = get_db_session()
        try:
            if claim_batch_for_analysis(batch_id, db):
                from app.tasks.analyze_batch import analyze_batch_task

                analyze_batch_task.delay(batch_id)
                logger.info(
                    "Batch #%d: all docs ready, batch analysis dispatched", batch_id
                )
        finally:
            db.close()

    return {"status": "success", "doc_id": doc_id}
