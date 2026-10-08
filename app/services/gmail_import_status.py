"""The user's Gmail import run as the API shows it (Import page, queue, Triage)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.core.timezone import now_utc
from app.schemas.gmail_import import GmailImportStatus
from app.services import gmail_runs

# How long a finished run stays on the queue and Triage banner, so a run that
# completes while you're elsewhere is still there when you look.
RECENT_RUN = timedelta(minutes=10)


def _iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def idle_status() -> GmailImportStatus:
    return GmailImportStatus(
        active=False,
        total=0,
        done=0,
        failed_count=0,
        sequential=True,
        cancelled=False,
        error=None,
        current_subject=None,
        waiting=False,
        started_at=None,
        finished_at=None,
    )


def status_from_state(state: dict[str, Any]) -> GmailImportStatus:
    return GmailImportStatus(
        active=gmail_runs.is_live(state),
        total=state["total"],
        done=state["done"],
        failed_count=len(state["failed"]),
        sequential=state["sequential"],
        cancelled=state["cancelled"],
        error=state.get("error"),
        current_subject=(state.get("current") or {}).get("subject"),
        waiting=bool(state.get("waiting_on")),
        started_at=_iso(state.get("started_at")),
        finished_at=_iso(state.get("finished_at")),
    )


def import_status(user_id: int) -> GmailImportStatus:
    state = gmail_runs.get_run("import", user_id)
    return status_from_state(state) if state else idle_status()


def recent_import_status(user_id: int) -> GmailImportStatus | None:
    """The run while it is live, or for ``RECENT_RUN`` after it ended; else None.

    Best-effort: with Redis down ``get_run`` yields nothing, so this is None.
    """
    state = gmail_runs.get_run("import", user_id)
    if not state or not state.get("total"):
        return None
    status = status_from_state(state)
    if status.active:
        return status
    # Abandoned (stale) runs have no finished_at; treat their last update as the end.
    ended = status.finished_at or _iso(state.get("updated_at"))
    if ended is None or now_utc() - ended > RECENT_RUN:
        return None
    return status
