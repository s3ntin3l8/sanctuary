"""Eager-aware Celery task dispatch for use from FastAPI request handlers.

Under CELERY_TASK_ALWAYS_EAGER=true (dev-without-Redis), task.delay() runs the
task body inline on the caller's thread. Calling that from a FastAPI handler
freezes the request thread (and everything queued behind it) for the entire
pipeline run. dispatch_task() spawns a daemon thread under EAGER, so the
handler returns immediately and HTMX polling stays alive.
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def dispatch_task(task: Any, *args: Any, **kwargs: Any) -> None:
    """Fire a Celery task without blocking the request thread.

    `task` may be a Celery task object or a dotted "module.path.task_name" string.
    """

    def _run() -> None:
        try:
            t = task
            if isinstance(t, str):
                module_path, name = t.rsplit(".", 1)
                t = getattr(importlib.import_module(module_path), name)
            t.apply_async(args=args, kwargs=kwargs)
        except Exception as exc:  # pragma: no cover - log and swallow
            label = task if isinstance(task, str) else getattr(task, "name", repr(task))
            logger.error("Dispatch %s failed: %s", label, exc, exc_info=True)
            _record_dispatch_failure(label, args, exc)

    if celery_app.conf.task_always_eager:
        threading.Thread(target=_run, daemon=True).start()
    else:
        _run()


def _record_dispatch_failure(label: str, args: tuple, exc: BaseException) -> None:
    """Mark the doc's stage failed when a dispatch silently dies.

    Without this, EAGER + concurrent dispatch losers (SQLite busy, import
    errors, etc.) leave stages stuck in PENDING with no UI signal.
    Best-effort: if we can't map label→stage or extract a doc_id, just exit.

    process_document_task is the retry_task for both EXTRACT and METADATA, so
    we resolve the actual retried stage by inspecting the doc's first
    non-terminal stage (matching the bundle retry endpoint's head logic).
    """
    try:
        from app.dependencies import get_db_session
        from app.models.database import Document
        from app.models.enums import StageStatus
        from app.services.pipeline_status import (
            _STAGE_ORDER,
            STAGE_REGISTRY,
            mark_failed_with_cascade,
        )

        candidate_stages = [
            s.stage for s in STAGE_REGISTRY.values() if s.retry_task == label
        ]
        if not candidate_stages or not args:
            return
        doc_id = args[0]
        if not isinstance(doc_id, int):
            return

        db = get_db_session()
        try:
            # When retry_task is unambiguous, use it. Otherwise inspect doc state
            # for the first non-terminal stage matching this retry_task.
            if len(candidate_stages) == 1:
                stage = candidate_stages[0]
            else:
                doc = db.query(Document).filter(Document.id == doc_id).first()
                if doc is None:
                    return
                stages = doc.pipeline_stages or {}
                terminal = {
                    StageStatus.COMPLETED.value,
                    StageStatus.SKIPPED.value,
                }
                stage = next(
                    (
                        s.stage
                        for s in _STAGE_ORDER
                        if s.stage in candidate_stages
                        and stages.get(s.stage.value, {}).get("status") not in terminal
                    ),
                    candidate_stages[0],
                )

            mark_failed_with_cascade(doc_id, stage, db, error=f"dispatch error: {exc}")
        finally:
            db.close()
    except Exception as inner:  # pragma: no cover - last-resort safety
        logger.error("dispatch failure recorder itself failed: %s", inner)
