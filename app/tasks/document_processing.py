import logging
from datetime import UTC, datetime

from app.config import SessionLocal
from app.models.database import Document, IngestStatus
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def process_document_task(self, doc_id: int):
    """Process a document in the background."""
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
            logger.info(f"Document {doc_id} processed successfully")
            return {"status": "success", "doc_id": doc_id}
        except Exception as e:
            doc.ingest_status = IngestStatus.FAILED
            doc.ingest_error = str(e)
            logger.error(f"Document {doc_id} processing failed: {e}")

            if self.request.retries < self.max_retries:
                raise self.retry(
                    exc=e, countdown=60 * (self.request.retries + 1)
                ) from e
            return {"status": "failed", "doc_id": doc_id, "error": str(e)}
        finally:
            doc.ingest_completed_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()


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
