import logging
from datetime import date

import httpx
from celery.exceptions import SoftTimeLimitExceeded

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _release(user_id: int, day: date) -> None:
    from app import config
    from app.services.intelligence.home_briefing_generator import release_claim

    db = config.SessionLocal()
    try:
        release_claim(db, user_id, day)
    except Exception as exc:
        logger.warning("Failed to release briefing claim for user %s: %s", user_id, exc)
    finally:
        db.close()


@celery_app.task(
    bind=True,
    max_retries=1,
    name="app.tasks.generate_home_briefing.generate_home_briefing_task",
)
def generate_home_briefing_task(self, user_id: int, day_iso: str):
    """Generate the user's briefing for ``day_iso``. Releases the dispatch
    claim on terminal exit only (not between retries)."""
    from app.services.intelligence.home_briefing_generator import (
        BriefingUserMissing,
        generate,
        mark_failed,
    )

    day = date.fromisoformat(day_iso)
    terminal = True
    try:
        generate(user_id, day)
        return {"status": "success", "user_id": user_id, "day": day_iso}
    except BriefingUserMissing as e:
        logger.warning("Briefing for user %s skipped: %s", user_id, e)
        return {"status": "not_found", "user_id": user_id}
    except ValueError as e:
        # call_json_ai: empty answer, thinking loop, schema violation. The
        # raw text points at debug files; the card gets a readable line.
        logger.warning("Briefing for user %s: unusable model answer: %s", user_id, e)
        mark_failed(user_id, day, "The model returned an unusable answer. Try again.")
        return {"status": "failed", "user_id": user_id, "error": str(e)}
    except (httpx.ReadTimeout, httpx.ConnectError) as e:
        if self.request.retries < self.max_retries:
            terminal = False
            raise self.retry(exc=e, countdown=60) from e
        mark_failed(user_id, day, f"provider unreachable: {e}")
        return {"status": "failed", "user_id": user_id, "error": str(e)}
    except SoftTimeLimitExceeded as e:
        mark_failed(user_id, day, f"soft time limit exceeded: {e}")
        return {"status": "failed", "user_id": user_id, "error": str(e)}
    except Exception as e:
        logger.error("Briefing for user %s failed: %s", user_id, e, exc_info=True)
        mark_failed(user_id, day, str(e))
        return {"status": "failed", "user_id": user_id, "error": str(e)}
    finally:
        if terminal:
            _release(user_id, day)
