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
    from app.services.embeddings import generate_embedding
    from app.services.pipeline_status import mark_completed, mark_failed, mark_started

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.EMBEDDINGS, db)
    finally:
        db.close()

    try:
        run_async(generate_embedding(doc_id))
    except Exception as e:
        logger.error(f"Embedding failed for doc {doc_id}: {e}", exc_info=True)
        db2 = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.EMBEDDINGS, db2, error=str(e))
        finally:
            db2.close()
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db3 = get_db_session()
    try:
        mark_completed(doc_id, PipelineStage.EMBEDDINGS, db3)
    finally:
        db3.close()

    return {"status": "success", "doc_id": doc_id}
