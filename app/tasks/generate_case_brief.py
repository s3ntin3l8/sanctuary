import logging

import httpx

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _release_brief_claim(case_id: str) -> None:
    """Clear cases.brief_queued_at via a fresh session so the next wave of
    pipeline activity can re-claim. Best-effort: errors are logged, not raised."""
    from app.config import SessionLocal
    from app.services.intelligence.orchestrator import release_case_brief_claim

    db = SessionLocal()
    try:
        release_case_brief_claim(case_id, db)
    except Exception as exc:
        logger.warning("Failed to release brief claim for case %s: %s", case_id, exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    max_retries=3,
    name="app.tasks.generate_case_brief.generate_case_brief_task",
)
def generate_case_brief_task(self, case_id: str):
    """Auto-triggered after extract_claims: regenerate case-level AI brief.

    Releases cases.brief_queued_at on terminal exit (success or final
    failure) — but NOT on Celery retry, so a parallel trigger can't race in
    during the retry countdown.
    """
    from app.services.intelligence.case_brief_generator import generate

    try:
        generate(case_id)
        return {"status": "success", "case_id": case_id}
    except ValueError as e:
        logger.warning("Case %s brief skipped: %s", case_id, e)
        return {"status": "not_found", "case_id": case_id}
    except httpx.ReadTimeout as e:
        if self.request.retries < 1:
            logger.info("Case %s brief timeout — retrying once in 90s", case_id)
            # Keep the brief_queued_at claim during the retry countdown so
            # parallel triggers stay blocked.
            raise self.retry(exc=e, countdown=90, max_retries=1) from e
        logger.warning(
            "Case %s brief timeout after retry (%s) — marking failed", case_id, e
        )
        return {"status": "failed", "case_id": case_id, "error": str(e)}
    except Exception as e:
        logger.error(f"Case {case_id} brief generation failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=60 * (self.request.retries + 1)) from e
        return {"status": "failed", "case_id": case_id, "error": str(e)}
    finally:
        # Only released on terminal exit. `self.retry()` raises celery.Retry
        # which short-circuits past this finally via Celery's internal task
        # machinery — but to be defensive, also skip release when a retry is
        # in flight by checking the current request state.
        if not _is_retry_in_flight(self):
            _release_brief_claim(case_id)


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
    except ValueError as e:
        logger.warning("Case %s brief skipped: %s", case_id, e)
        return {"status": "not_found", "case_id": case_id}
    except httpx.ReadTimeout as e:
        if self.request.retries < 1:
            logger.info("Case %s brief refresh timeout — retrying once in 90s", case_id)
            raise self.retry(exc=e, countdown=90, max_retries=1) from e
        logger.warning("Case %s brief refresh timeout after retry (%s)", case_id, e)
        return {"status": "failed", "case_id": case_id, "error": str(e)}
    except Exception as e:
        logger.error(f"Case {case_id} brief refresh failed: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=30) from e
        return {"status": "failed", "case_id": case_id, "error": str(e)}
    finally:
        if not _is_retry_in_flight(self):
            _release_brief_claim(case_id)


def _is_retry_in_flight(task) -> bool:
    """True when the task is exiting via self.retry() (the Retry exception is
    being raised through the finally block). Celery doesn't expose this
    cleanly, but the active exception type does."""
    import sys

    from celery.exceptions import Retry

    exc = sys.exc_info()[1]
    return isinstance(exc, Retry)
