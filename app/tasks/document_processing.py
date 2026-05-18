import logging
import time

import httpx
from sqlalchemy.exc import OperationalError as SA_OperationalError

from app.dependencies import get_db_session
from app.models.database import Document
from app.models.enums import PipelineStage
from app.services.pipeline_status import is_db_locked, stages_dict
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

_TRANSIENT_AI_ERRORS = (
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadError,
)


@celery_app.task(bind=True, max_retries=3)
def process_document_task(self, doc_id: int):
    """Process a document: Docling conversion, then trigger Phase 4 AI pipeline."""
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed_with_cascade,
        mark_started,
        schedule_retry,
    )

    logger.info("Doc #%d: processing task started", doc_id)
    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Document {doc_id} not found")
            return {"status": "not_found", "doc_id": doc_id}

        # Skip EXTRACT when retrying a later stage (EXTRACT already completed).
        stages = stages_dict(doc)
        extract_done = (
            stages.get(PipelineStage.EXTRACT.value, {}).get("status") == "completed"
        )

        if not extract_done:
            from app.services.ingestion.service import (
                IngestionError,
                process_uploaded_document,
            )

            mark_started(doc_id, PipelineStage.EXTRACT, db)
            try:
                process_uploaded_document(doc, db)
                mark_completed(doc_id, PipelineStage.EXTRACT, db)
                logger.info(f"Document {doc_id} extracted successfully")
            except IngestionError as e:
                db.rollback()
                error_msg = f"Ingestion error: {e.message}"
                if e.detail:
                    error_msg += f" ({e.detail})"
                mark_failed_with_cascade(
                    doc_id, PipelineStage.EXTRACT, db, error=error_msg
                )
                logger.warning(f"Document {doc_id} ingestion failed: {e}")
                return {"status": "failed", "doc_id": doc_id, "error": str(e)}
            except Exception as e:
                db.rollback()
                logger.error(f"Document {doc_id} processing failed: {e}", exc_info=True)

                if self.request.retries < self.max_retries:
                    countdown = 60 * (self.request.retries + 1)
                    # Mark RETRYING (not FAILED) so the polling templates keep
                    # refreshing through the countdown — the next attempt runs
                    # invisibly otherwise.
                    schedule_retry(
                        doc_id,
                        PipelineStage.EXTRACT,
                        db,
                        error=f"System error: {e}",
                        attempt=self.request.retries + 1,
                        max_attempts=self.max_retries,
                        countdown=countdown,
                    )
                    raise self.retry(exc=e, countdown=countdown) from e

                # All retries exhausted — terminal failure cascades downstream.
                mark_failed_with_cascade(
                    doc_id, PipelineStage.EXTRACT, db, error=f"System error: {e}"
                )
                from celery.exceptions import MaxRetriesExceededError

                raise MaxRetriesExceededError(
                    f"Document {doc_id} failed after {self.max_retries} retries",
                    exc=e,
                ) from e
            except BaseException as e:
                db.rollback()
                mark_failed_with_cascade(
                    doc_id,
                    PipelineStage.EXTRACT,
                    db,
                    error=f"task aborted: {type(e).__name__}",
                )
                raise

        # Phase 1: metadata extraction + auto-triage (with transient-error retry).
        # Skip when already completed — recovery may re-dispatch this task for a
        # later stuck stage; re-running the AI call here would be wasteful.
        metadata_done = (
            stages.get(PipelineStage.METADATA.value, {}).get("status") == "completed"
        )
        if not metadata_done:
            _run_phase1_summary(doc_id)

        # Gate: if METADATA ended failed, skip downstream dispatch for this doc.
        # Sibling docs still proceed via the batch analyzer (see below).
        db.refresh(doc)
        metadata_status = (
            stages_dict(doc)
            .get(PipelineStage.METADATA.value, {})
            .get("status", "pending")
        )
        if metadata_status == "failed":
            logger.warning(
                f"Doc {doc_id}: METADATA failed after retries — skipping downstream dispatch"
            )
            return {"status": "metadata_failed", "doc_id": doc_id}

        # Batch analysis gate: when every doc in this batch has completed METADATA,
        # claim and dispatch the batch analyzer. Uses an atomic CAS so only one
        # worker fires the task even when multiple docs finish near-simultaneously.
        if doc.ingest_batch_id:
            from app.services.intelligence.orchestrator import claim_batch_for_analysis

            db_batch = get_db_session()
            try:
                if claim_batch_for_analysis(doc.ingest_batch_id, db_batch):
                    from app.tasks.analyze_batch import analyze_batch_task

                    analyze_batch_task.delay(doc.ingest_batch_id)
                    logger.info(
                        "Doc #%d: batch #%d ready — dispatched analyze_batch_task",
                        doc_id,
                        doc.ingest_batch_id,
                    )
            finally:
                db_batch.close()

        # Embeddings — claim before dispatch for the same fan-out protection.
        from app.services.pipeline_status import claim_stage_for_dispatch

        db_emb = get_db_session()
        try:
            if claim_stage_for_dispatch(doc_id, PipelineStage.EMBEDDINGS, db_emb):
                try:
                    from app.tasks.generate_embedding import generate_embedding_task

                    generate_embedding_task.delay(doc_id)
                except Exception as e:
                    logger.error(
                        "Doc #%d: generate_embedding dispatch raised — marking stage failed: %s",
                        doc_id,
                        e,
                        exc_info=True,
                    )
                    mark_failed_with_cascade(
                        doc_id,
                        PipelineStage.EMBEDDINGS,
                        db,
                        error=f"dispatch failed: {e}",
                    )
            else:
                logger.debug(
                    "Doc #%d: EMBEDDINGS already claimed — skipping dispatch", doc_id
                )
        finally:
            db_emb.close()

        return {"status": "success", "doc_id": doc_id}
    finally:
        db.close()


_METADATA_MAX_RETRIES = 3
_METADATA_BACKOFF = [10, 30, 60]  # seconds between attempts 1→2, 2→3, and final


def _run_phase1_summary(doc_id: int) -> None:
    """Run Phase 1 metadata extraction with transient-error retry.

    Retries up to _METADATA_MAX_RETRIES times on network/timeout errors.
    Non-transient exceptions mark the stage failed immediately (no retry).

    Status transitions on transient errors:
        running → retrying (during sleep) → running (next attempt) → … → completed | failed
    """
    from app.models.enums import PipelineStage
    from app.services.ai_summary import _summarize_document_sync
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_started,
        schedule_retry,
    )

    db2 = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.METADATA, db2)
    finally:
        db2.close()

    last_error: Exception | None = None
    try:
        for attempt in range(_METADATA_MAX_RETRIES):
            db2 = get_db_session()
            try:
                _summarize_document_sync(doc_id, db2)
                mark_completed(doc_id, PipelineStage.METADATA, db2)
                return
            except _TRANSIENT_AI_ERRORS + (SA_OperationalError,) as e:
                if isinstance(e, SA_OperationalError) and not is_db_locked(e):
                    db2.rollback()
                    mark_failed(doc_id, PipelineStage.METADATA, db2, error=str(e))
                    logger.warning(f"Phase 1 summary failed for doc {doc_id}: {e}")
                    return
                last_error = e
                db2.close()
                if attempt < _METADATA_MAX_RETRIES - 1:
                    wait = _METADATA_BACKOFF[attempt]
                    logger.info(
                        f"Doc {doc_id}: METADATA transient error (attempt {attempt + 1}/{_METADATA_MAX_RETRIES}), "
                        f"retrying in {wait}s: {e}"
                    )
                    # Flip to RETRYING so the UI shows "Retrying METADATA (N/M) in Ws"
                    # instead of a silent spin.
                    db_retry = get_db_session()
                    try:
                        schedule_retry(
                            doc_id,
                            PipelineStage.METADATA,
                            db_retry,
                            error=str(e),
                            attempt=attempt + 1,
                            max_attempts=_METADATA_MAX_RETRIES,
                            countdown=wait,
                        )
                    finally:
                        db_retry.close()
                    time.sleep(wait)
                    # Flip back to RUNNING for the next attempt — keeps the
                    # Celery and in-process retry semantics consistent.
                    db_start = get_db_session()
                    try:
                        mark_started(doc_id, PipelineStage.METADATA, db_start)
                    finally:
                        db_start.close()
            except Exception as e:
                db2.rollback()
                mark_failed(doc_id, PipelineStage.METADATA, db2, error=str(e))
                logger.warning(f"Phase 1 summary failed for doc {doc_id}: {e}")
                return
            finally:
                db2.close()
    except BaseException as e:
        if not isinstance(e, Exception):
            _db = get_db_session()
            try:
                mark_failed(
                    doc_id,
                    PipelineStage.METADATA,
                    _db,
                    error=f"task aborted: {type(e).__name__}",
                )
            except Exception:
                pass
            finally:
                _db.close()
        raise

    # All transient retries exhausted
    db2 = get_db_session()
    try:
        mark_failed(
            doc_id,
            PipelineStage.METADATA,
            db2,
            error=f"timeout after {_METADATA_MAX_RETRIES} attempts: {last_error}",
        )
        logger.warning(
            f"Doc {doc_id}: METADATA failed after {_METADATA_MAX_RETRIES} attempts: {last_error}"
        )
    finally:
        db2.close()


@celery_app.task
def reingest_all_documents_task(case_id: str | None = None):
    """Re-ingest all documents for a case (or all cases if case_id is None)."""
    db = get_db_session()
    try:
        query = db.query(Document)
        if case_id:
            query = query.filter(Document.case_id == case_id)

        docs = query.all()
        for doc in docs:
            process_document_task.delay(doc.id)

        return {"status": "queued", "count": len(docs), "case_id": case_id}
    finally:
        db.close()
