import logging
from datetime import UTC, datetime

from app.config import SessionLocal
from app.models.database import Document
from app.models.enums import PipelineState
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def generate_document_summary_task(self, doc_id: int):
    """Generate AI summary for a document in the background."""
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Document {doc_id} not found for summary generation")
            return {"status": "not_found", "doc_id": doc_id}

        if not doc.content:
            logger.info(f"Document {doc_id} has no content to summarize")
            return {"status": "skipped", "doc_id": doc_id, "reason": "no_content"}

        from app.services.ai_summary import summarize_document

        try:
            from app.core.async_utils import run_async

            run_async(summarize_document(doc, db))
            # The async summarize_document already handles assignment and commit if successful,
            # but here it's being used as a helper.
            # Actually, summarize_document returns the doc.
            db.commit()
            doc.ai_summary_created_at = datetime.now(UTC)
            db.commit()
            logger.info(f"Summary generated for document {doc_id}")
            return {"status": "success", "doc_id": doc_id}
        except Exception as e:
            logger.error(f"Summary generation failed for document {doc_id}: {e}")

            if self.request.retries < self.max_retries:
                raise self.retry(
                    exc=e, countdown=120 * (self.request.retries + 1)
                ) from e
            return {"status": "failed", "doc_id": doc_id, "error": str(e)}
    finally:
        db.close()


@celery_app.task
def generate_summaries_for_case_task(case_id: str):
    """Generate summaries for all documents in a case that lack enrichment."""
    db = SessionLocal()
    try:
        docs = (
            db.query(Document)
            .filter(
                Document.case_id == case_id,
                Document.content.isnot(None),
                Document.pipeline_state != PipelineState.COMPLETED,
            )
            .all()
        )

        for doc in docs:
            generate_document_summary_task.delay(doc.id)

        return {"status": "queued", "count": len(docs), "case_id": case_id}
    finally:
        db.close()
