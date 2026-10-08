"""Shared Celery retry for transient AI HTTP errors (5xx, warm-up 4xx)."""

import logging

import httpx

from app.dependencies import get_db_session
from app.models.enums import PipelineStage
from app.services.intelligence._ai_call import is_transient_ai_http_error
from app.services.pipeline_status import schedule_retry

logger = logging.getLogger(__name__)


def retry_if_transient_ai_error(
    task, doc_id: int, stage: PipelineStage, exc: Exception
) -> None:
    """Re-queue ``task`` with linear backoff when ``exc`` is a transient AI HTTP
    error and retries remain; otherwise return so the caller fails the stage.

    Raises ``celery.exceptions.Retry`` on the retry path. Call from inside the
    task's generic ``except Exception`` branch — a sibling ``except`` clause
    cannot catch an exception raised from another one.
    """
    if not isinstance(exc, httpx.HTTPStatusError) or not is_transient_ai_http_error(
        exc
    ):
        return
    if task.request.retries >= task.max_retries:
        return
    countdown = 60 * (task.request.retries + 1)
    logger.warning(
        "Doc #%d: %s AI returned HTTP %d (transient) — retry %d in %ds: %s",
        doc_id,
        stage.value,
        exc.response.status_code,
        task.request.retries + 1,
        countdown,
        exc,
    )
    db = get_db_session()
    try:
        schedule_retry(
            doc_id,
            stage,
            db,
            error=str(exc),
            attempt=task.request.retries + 1,
            max_attempts=task.max_retries,
            countdown=countdown,
        )
    finally:
        db.close()
    raise task.retry(exc=exc, countdown=countdown) from exc
