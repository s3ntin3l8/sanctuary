import logging
import os

from app.dependencies import get_db_session
from app.models.database import Document
from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def process_document_task(self, doc_id: int):
    """Process a document: Docling conversion, then trigger Phase 4 AI pipeline."""
    from app.services.pipeline_status import mark_completed, mark_failed, mark_started

    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Document {doc_id} not found")
            return {"status": "not_found", "doc_id": doc_id}

        from app.services.ingestion import IngestionError, process_uploaded_document

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
            mark_failed(doc_id, PipelineStage.EXTRACT, db, error=error_msg)
            logger.warning(f"Document {doc_id} ingestion failed: {e}")
            return {"status": "failed", "doc_id": doc_id, "error": str(e)}
        except Exception as e:
            db.rollback()
            mark_failed(
                doc_id, PipelineStage.EXTRACT, db, error=f"System error: {str(e)}"
            )
            logger.error(f"Document {doc_id} processing failed: {e}", exc_info=True)

            if self.request.retries < self.max_retries:
                raise self.retry(
                    exc=e, countdown=60 * (self.request.retries + 1)
                ) from e
            from celery.exceptions import MaxRetriesExceededError

            raise MaxRetriesExceededError(
                f"Document {doc_id} failed after {self.max_retries} retries",
                exc=e,
            ) from e

        # For EML files: extract PDF/document attachments as sibling Documents in
        # the same batch BEFORE Phase 1 so that when child tasks run eagerly they
        # can't claim the batch before the parent's Phase 1 has run.
        child_ids: list[int] = []
        file_ext = os.path.splitext(doc.file_path or "")[1].lower()
        if file_ext == ".eml":
            from app.services.ingestion.service import extract_eml_attachments

            child_ids = extract_eml_attachments(doc, db)
            if child_ids:
                logger.info(
                    f"Document {doc_id}: extracted {len(child_ids)} EML attachment(s)"
                )

        # Phase 1: metadata extraction + auto-triage (run for the parent before
        # child tasks are dispatched so enrichment from children can't race it)
        _run_phase1_summary(doc_id)

        # Queue attachment processing tasks.  With CELERY_TASK_ALWAYS_EAGER the
        # child tasks run synchronously here; the last one to complete will claim
        # the batch and dispatch analyze_batch_task for the whole group.
        for child_id in child_ids:
            process_document_task.delay(child_id)

        # Batch-ready gating: if all docs in this batch are done, claim and dispatch batch analysis
        batch_id = doc.ingest_batch_id
        if batch_id:
            from app.services.intelligence.orchestrator import claim_batch_for_analysis
            from app.tasks.analyze_batch import analyze_batch_task

            if claim_batch_for_analysis(batch_id, db):
                logger.info(
                    f"Batch {batch_id}: all docs done, dispatching analyze_batch_task"
                )
                analyze_batch_task.delay(batch_id)
        else:
            # No batch — dispatch enrichment directly
            from app.tasks.enrich_document import enrich_document_task

            enrich_document_task.delay(doc_id)

        # Embeddings — now a real Celery task for proper stage tracking
        from app.tasks.generate_embedding import generate_embedding_task

        generate_embedding_task.delay(doc_id)

        return {"status": "success", "doc_id": doc_id}
    finally:
        db.close()


def _run_phase1_summary(doc_id: int) -> None:
    """Run Phase 1 metadata extraction (az_court, sender, received_date, originator_type)."""
    from app.models.enums import PipelineStage
    from app.services.pipeline_status import mark_completed, mark_failed, mark_started

    db2 = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.METADATA, db2)
        try:
            from app.services.ai_summary import _summarize_document_sync

            _summarize_document_sync(doc_id, db2)
            mark_completed(doc_id, PipelineStage.METADATA, db2)
        except Exception as e:
            mark_failed(doc_id, PipelineStage.METADATA, db2, error=str(e))
            logger.warning(f"Phase 1 summary failed for doc {doc_id}: {e}")
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
