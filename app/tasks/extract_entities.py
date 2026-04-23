import logging

from app.models.enums import PipelineStage
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.extract_entities.extract_entities_task")
def extract_entities_task(doc_id: int):
    """Extract named entities (persons, courts, law firms, citations) from a document."""
    from app.dependencies import get_db_session
    from app.services.intelligence.entity_extractor import extract
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_skipped,
        mark_started,
    )

    db = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.ENTITIES, db)
    finally:
        db.close()

    logger.info("Doc #%d: entities started", doc_id)
    try:
        skipped = extract(doc_id)
    except Exception as e:
        logger.error(f"Doc {doc_id} entity extraction task failed: {e}", exc_info=True)
        db = get_db_session()
        try:
            mark_failed(doc_id, PipelineStage.ENTITIES, db, error=str(e))
        finally:
            db.close()
        return {"status": "failed", "doc_id": doc_id, "error": str(e)}

    db = get_db_session()
    try:
        if skipped:
            mark_skipped(doc_id, PipelineStage.ENTITIES, db, reason=skipped)
        else:
            mark_completed(doc_id, PipelineStage.ENTITIES, db)
    finally:
        db.close()

    logger.info(
        "Doc #%d: entities %s",
        doc_id,
        f"skipped ({skipped})" if skipped else "complete",
    )
    return {"status": "success", "doc_id": doc_id}
