import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.generate_case_brief.generate_case_brief_task",
)
def generate_case_brief_task(self, case_id: str):
    """Auto-triggered after extract_claims: regenerate case-level AI brief."""
    from app.services.intelligence.case_brief_generator import generate

    try:
        generate(case_id)
        return {"status": "success", "case_id": case_id}
    except Exception as e:
        logger.error(f"Case {case_id} brief generation failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "case_id": case_id, "error": str(e)}


@celery_app.task(
    bind=True,
    max_retries=1,
    name="app.tasks.generate_case_brief.refresh_case_brief_task",
)
def refresh_case_brief_task(self, case_id: str):
    """Manual refresh triggered from dashboard UI."""
    from app.services.intelligence.case_brief_generator import generate

    try:
        generate(case_id)
        return {"status": "success", "case_id": case_id}
    except Exception as e:
        logger.error(f"Case {case_id} brief refresh failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=30) from e
        return {"status": "failed", "case_id": case_id, "error": str(e)}
