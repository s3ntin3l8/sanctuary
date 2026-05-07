import logging

import httpx

from app.config import SessionLocal
from app.models.database import Document
from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, max_retries=3, name="app.tasks.analyze_batch.analyze_batch_task"
)
def analyze_batch_task(self, batch_id: int):
    """Run batch-level AI analysis (cover-letter detection + action items), then enqueue per-doc enrichment."""
    from app.services.intelligence.batch_analyzer import analyze
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_skipped,
        mark_started,
        schedule_retry,
    )

    # Fetch doc IDs for stage tracking
    db = SessionLocal()
    try:
        doc_ids = [
            r[0]
            for r in db.query(Document.id)
            .filter(Document.ingest_batch_id == batch_id)
            .all()
        ]
        for doc_id in doc_ids:
            mark_started(doc_id, PipelineStage.BATCH_ANALYSIS, db)
    finally:
        db.close()

    logger.info("Batch #%d: batch_analysis started (%d docs)", batch_id, len(doc_ids))
    try:
        ran = analyze(batch_id)
    except httpx.ReadTimeout as e:
        if self.request.retries < 1:
            logger.info(
                "Batch #%d: batch_analysis timeout — retrying once in 90s", batch_id
            )
            db = SessionLocal()
            try:
                for doc_id in doc_ids:
                    schedule_retry(
                        doc_id,
                        PipelineStage.BATCH_ANALYSIS,
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
            "Batch #%d: batch_analysis timeout after retry (%s) — marking failed",
            batch_id,
            e,
        )
        db = SessionLocal()
        try:
            for doc_id in doc_ids:
                mark_failed(
                    doc_id, PipelineStage.BATCH_ANALYSIS, db, error=f"timeout: {e}"
                )
        finally:
            db.close()
        return {"status": "failed", "batch_id": batch_id, "error": str(e)}
    except Exception as e:
        logger.error(f"Batch {batch_id} analysis failed: {e}", exc_info=True)
        if self.request.retries < self.max_retries:
            countdown = 60 * (self.request.retries + 1)
            db = SessionLocal()
            try:
                for doc_id in doc_ids:
                    schedule_retry(
                        doc_id,
                        PipelineStage.BATCH_ANALYSIS,
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
        db = SessionLocal()
        try:
            for doc_id in doc_ids:
                mark_failed(doc_id, PipelineStage.BATCH_ANALYSIS, db, error=str(e))
        finally:
            db.close()
        logger.info(
            "Batch #%d: batch_analysis failed — still enqueueing enrich for %d doc(s)",
            batch_id,
            len(doc_ids),
        )
        for doc_id in doc_ids:
            _enrich_if_pending(doc_id)
        return {"status": "failed", "batch_id": batch_id, "error": str(e)}

    db = SessionLocal()
    try:
        for doc_id in doc_ids:
            if ran:
                mark_completed(doc_id, PipelineStage.BATCH_ANALYSIS, db)
            else:
                mark_skipped(
                    doc_id,
                    PipelineStage.BATCH_ANALYSIS,
                    db,
                    reason="single-doc or empty batch",
                )
    finally:
        db.close()

    logger.info(
        "Batch #%d: batch_analysis %s — enqueueing enrich for %d doc(s)",
        batch_id,
        "complete" if ran else "skipped",
        len(doc_ids),
    )
    for doc_id in doc_ids:
        _enrich_if_pending(doc_id)

    return {"status": "success", "batch_id": batch_id, "enqueued_docs": len(doc_ids)}


def _enrich_if_pending(doc_id: int) -> None:
    """Dispatch enrich only when the stage is currently pending — prevents fan-out."""
    db = SessionLocal()
    try:
        stages = (
            db.query(Document.pipeline_stages).filter(Document.id == doc_id).scalar()
        ) or {}
        if stages.get("enrich", {}).get("status") == "pending":
            enrich_document_task.delay(doc_id)
    finally:
        db.close()


from app.tasks.enrich_document import (
    enrich_document_task,  # noqa: E402 — avoids circular at task-registration time
)
