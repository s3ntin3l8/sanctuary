import asyncio
import logging

from app.services import user_settings_service
from app.services.intelligence.claim_dedup_judge import find_duplicates_for_case
from app.tasks.celery_app import SessionLocal, celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.claim_dedup.claim_dedup_task", queue="ai")
def claim_dedup_task(case_id: str) -> dict:
    db = SessionLocal()
    try:
        stats = asyncio.run(find_duplicates_for_case(case_id, db))
        db.commit()
        user_settings_service.set_dedup_result(case_id, stats, db)
        db.commit()
        return {"status": "done", **stats}
    except Exception as exc:
        logger.error("Claim dedup failed for %s: %s", case_id, exc, exc_info=True)
        try:
            user_settings_service.set_dedup_result(case_id, None, db, failed=True)
            db.commit()
        except Exception:
            pass
        return {"status": "failed", "error": str(exc)}
    finally:
        db.close()
