import logging
import time

import httpx

from app.dependencies import get_db_session
from app.models.database import Document
from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

_TRANSIENT_AI_ERRORS = (
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
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
        stages = doc.pipeline_stages or {}
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

        # Phase 1: metadata extraction + auto-triage (with transient-error retry)
        _run_phase1_summary(doc_id)

        # Gate: if METADATA ended failed, skip downstream dispatch for this doc.
        # Sibling docs still proceed via the batch analyzer (see below).
        db.refresh(doc)
        metadata_status = (
            (doc.pipeline_stages or {})
            .get(PipelineStage.METADATA.value, {})
            .get("status", "pending")
        )
        if metadata_status == "failed":
            logger.warning(
                f"Doc {doc_id}: METADATA failed after retries — skipping downstream dispatch"
            )
            return {"status": "metadata_failed", "doc_id": doc_id}

        # Proceeding-ready gating: metadata is done, now check for proceeding changes.
        # This replaces the direct batch analyzer call — the proceeding analyzer
        # will trigger the batch analyzer once it finishes (or skips).
        try:
            from app.tasks.analyze_proceeding import analyze_proceeding_task

            analyze_proceeding_task.delay(doc_id)
        except Exception as e:
            logger.error(
                "Doc #%d: analyze_proceeding dispatch raised — marking stage failed: %s",
                doc_id,
                e,
                exc_info=True,
            )
            mark_failed_with_cascade(
                doc_id,
                PipelineStage.PROCEEDING_ANALYSIS,
                db,
                error=f"dispatch failed: {e}",
            )

        # Embeddings — now a real Celery task for proper stage tracking
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
    for attempt in range(_METADATA_MAX_RETRIES):
        db2 = get_db_session()
        try:
            _summarize_document_sync(doc_id, db2)
            mark_completed(doc_id, PipelineStage.METADATA, db2)
            return
        except _TRANSIENT_AI_ERRORS as e:
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
