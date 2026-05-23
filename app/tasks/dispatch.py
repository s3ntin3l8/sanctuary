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
            result = t.apply_async(args=args, kwargs=kwargs)
            # Diagnostic log line — pairs with dispatch_pipeline_retry's
            # pre-dispatch info log so we can correlate which dispatches
            # made it to the broker and got a task_id assigned.
            label = task if isinstance(task, str) else getattr(task, "name", repr(task))
            logger.info(
                "dispatch_task: %s args=%s task_id=%s",
                label,
                args,
                getattr(result, "id", None),
            )
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

    Each PipelineStage now maps to exactly one retry_task, so label uniquely
    identifies the failing stage.
    """
    try:
        from app.dependencies import get_db_session
        from app.models.database import Document
        from app.models.enums import StageStatus
        from app.services.pipeline_status import (
            STAGE_REGISTRY,
            mark_failed_with_cascade,
            stages_dict,
        )

        # Only doc-keyed dispatches map cleanly to a doc's stage. Batch-keyed
        # tasks (analyze_batch_task) take a batch_id that could collide with an
        # unrelated doc_id — skip rather than corrupt that doc's state.
        candidates = [
            s
            for s in STAGE_REGISTRY.values()
            if s.retry_task == label and s.dispatch_arg != "batch_id"
        ]
        if not candidates or not args:
            return
        doc_id = args[0]
        if not isinstance(doc_id, int):
            return

        # Invariant: STAGE_REGISTRY assigns one retry_task per stage, so a
        # doc-keyed dispatch label resolves to a single stage.
        assert len(candidates) == 1, (
            f"dispatch label {label!r} maps to {len(candidates)} stages; "
            "STAGE_REGISTRY must keep retry_task unique per doc-keyed stage"
        )
        stage = candidates[0].stage

        db = get_db_session()
        try:
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if doc is None:
                return
            stages = stages_dict(doc)

            # If the task's own error handler already marked this stage failed,
            # leave its specific message alone — don't stomp it with a generic
            # "dispatch error: ..." that erases the real cause.
            if stages.get(stage.value, {}).get("status") == StageStatus.FAILED.value:
                return

            mark_failed_with_cascade(doc_id, stage, db, error=f"dispatch error: {exc}")
        finally:
            db.close()
    except Exception as inner:  # pragma: no cover - last-resort safety
        logger.error("dispatch failure recorder itself failed: %s", inner)
