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

    if celery_app.conf.task_always_eager:
        threading.Thread(target=_run, daemon=True).start()
    else:
        _run()
