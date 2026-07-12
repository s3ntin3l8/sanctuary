import logging

from app.core.async_utils import run_async
from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.generate_embedding.generate_embedding_task",
)
def generate_embedding_task(self, doc_id: int):
    """Generate and store vector embedding for a document."""
    from app.dependencies import get_db_session
    from app.models.database import Document
    from app.models.enums import StageStatus
    from app.services.embeddings import generate_embedding
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_started,
        schedule_retry,
        stages_dict,
    )

    _TERMINAL = {
        StageStatus.COMPLETED.value,
        StageStatus.FAILED.value,
        StageStatus.SKIPPED.value,
    }

    # Dependency gate: METADATA must be terminal before EMBEDDINGS runs.
    # EMBEDDINGS reads doc.content (from EXTRACT) and uses doc.title
    # (from METADATA). dispatch_batch_retry fires EMBEDDINGS in parallel
    # with the head-stage retry — when head=EXTRACT, both get queued at
    # once, and if the EMBEDDINGS worker picks up first it would run
    # against stale or missing content. process_document_task already
    # dispatches EMBEDDINGS only after METADATA-done, so this gate makes
    # the retry path symmetric.
    #
    # Return early WITHOUT marking the stage — leave it PENDING so
    # process_document_task's claim_stage_for_dispatch (line 151-160)
    # picks it up again after METADATA completes. Marking SKIPPED would
    # break the re-dispatch (claim_stage_for_dispatch only claims pending
    # rows) and lose the embedding for this doc forever.
    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if doc:
            stages = stages_dict(doc)
            metadata_status = stages.get("metadata", {}).get("status")
            if metadata_status not in _TERMINAL:
                logger.info(
                    "Doc #%d: deferring embeddings — METADATA not yet terminal "
                    "(status=%s); process_document_task will redispatch on completion",
                    doc_id,
                    metadata_status,
                )
                return {
                    "status": "deferred",
                    "doc_id": doc_id,
                    "reason": "metadata_not_completed",
                }
    finally:
        db.close()

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.EMBEDDINGS, db)
    finally:
        db.close()

    logger.info("Doc #%d: embeddings started", doc_id)
    try:
        run_async(generate_embedding(doc_id))
    except Exception as e:
        logger.error(f"Embedding failed for doc {doc_id}: {e}", exc_info=True)
        if self.request.retries < self.max_retries:
            countdown = 60 * (self.request.retries + 1)
            db2 = get_db_session()
            try:
                schedule_retry(
                    doc_id,
                    PipelineStage.EMBEDDINGS,
                    db2,
                    error=str(e),
                    attempt=self.request.retries + 1,
                    max_attempts=self.max_retries,
                    countdown=countdown,
                )
            finally:
                db2.close()
            raise self.retry(exc=e, countdown=countdown) from e

        # All retries exhausted — terminal failure.
        db2 = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.EMBEDDINGS, db2, error=str(e))
        finally:
            db2.close()
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db3 = get_db_session()
    try:
        mark_completed(doc_id, PipelineStage.EMBEDDINGS, db3)
    finally:
        db3.close()

    logger.info("Doc #%d: embeddings complete", doc_id)
    return {"status": "success", "doc_id": doc_id}


@celery_app.task(
    bind=True,
    name="app.tasks.generate_embedding.reindex_all_embeddings_task",
)
def reindex_all_embeddings_task(self):
    """Regenerate embeddings for every doc with content. Writes progress to
    UserSettings.reindex_job so the HTMX poller in Settings → AI can render
    a live bar.

    The HTTP route handles the embedding-column resize DDL synchronously
    before dispatching this task, so this task only loops over docs.
    """
    from app.dependencies import get_db_session
    from app.services.embeddings import reindex_all_docs
    from app.services.user_settings_service import (
        set_reindex_done,
        set_reindex_failed,
        update_reindex_progress,
    )

    db = get_db_session()
    try:

        def _on_progress(*, reindexed: int, failed: int) -> None:
            update_reindex_progress(db, reindexed=reindexed, failed=failed)

        try:
            result = run_async(reindex_all_docs(db, progress_cb=_on_progress))
        except Exception as exc:
            logger.exception("reindex_all_embeddings_task failed")
            set_reindex_failed(db, str(exc))
            raise

        set_reindex_done(db)
        return result
    finally:
        db.close()
