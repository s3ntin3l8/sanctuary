import logging
from datetime import UTC, datetime

from app.config import SessionLocal
from app.models.database import Document, IngestStatus
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def process_document_task(self, doc_id: int):
    """Process a document: Docling conversion, then trigger Phase 4 AI pipeline."""
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Document {doc_id} not found")
            return {"status": "not_found", "doc_id": doc_id}

        doc.ingest_status = IngestStatus.PROCESSING
        doc.ingest_started_at = datetime.now(UTC)
        db.commit()

        from app.services.ingestion import process_uploaded_document

        try:
            process_uploaded_document(doc, db)
            doc.ingest_status = IngestStatus.COMPLETED
            doc.ingest_completed_at = datetime.now(UTC)
            db.commit()
            logger.info(f"Document {doc_id} processed successfully")
        except Exception as e:
            doc.ingest_status = IngestStatus.FAILED
            doc.ingest_error = str(e)
            doc.ingest_completed_at = datetime.now(UTC)
            db.commit()
            logger.error(f"Document {doc_id} processing failed: {e}")

            if self.request.retries < self.max_retries:
                raise self.retry(
                    exc=e, countdown=60 * (self.request.retries + 1)
                ) from e
            return {"status": "failed", "doc_id": doc_id, "error": str(e)}

        # Phase 1: metadata extraction + auto-triage
        _run_phase1_summary(doc_id)

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

        return {"status": "success", "doc_id": doc_id}
    finally:
        db.close()


def _run_phase1_summary(doc_id: int) -> None:
    """Run Phase 1 metadata extraction (az_court, sender, received_date, originator_type)."""
    try:
        from app.config import SessionLocal as _SL
        from app.services.ai_summary import _summarize_document_sync

        db2 = _SL()
        try:
            _summarize_document_sync(doc_id, db2)
        finally:
            db2.close()
    except Exception as e:
        logger.warning(f"Phase 1 summary failed for doc {doc_id}: {e}")


@celery_app.task
def reingest_all_documents_task(case_id: str | None = None):
    """Re-ingest all documents for a case (or all cases if case_id is None)."""
    db = SessionLocal()
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
