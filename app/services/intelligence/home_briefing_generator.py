"""The Home "Morning briefing": a few sentences from the local model about
what needs the user today, cached per user and day in ``home_briefings``
(issue #175).

Follows ``case_brief_generator``: the row is the dispatch claim and the
cache. ``claim_for_dispatch`` wins exactly once per (user, day) while
``queued_at`` is NULL; the task writes the result and clears the claim on
terminal exit, so a Home polled every few seconds never fans out duplicate
AI calls.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import cast

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from app import config
from app.config import CELERY_TASK_TIME_LIMIT
from app.models.database import HomeBriefing, User
from app.services.ai_config import get_chat_config, is_external_endpoint
from app.services.home_service import HomeService
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import HOME_BRIEFING_SYSTEM, sanitize_oneline
from app.services.intelligence.schemas import HomeBriefingOut
from app.services.timezone_service import get_user_tz

logger = logging.getLogger(__name__)


class BriefingUserMissing(LookupError):
    """The user row is gone; nothing to brief."""


def today_for_user() -> date:
    """The calendar day in the configured timezone — a briefing is "today's"
    by the reader's clock, not UTC's."""
    return datetime.now(get_user_tz()).date()


def claim_for_dispatch(db: Session, user_id: int, day: date) -> bool:
    """Create or re-arm today's row and return True for the one caller that
    gets to dispatch the task.

    Inserts a ``processing`` row when there is none, or resets a finished /
    failed one to ``processing`` — but only while ``queued_at`` is NULL, so
    a run already in flight is never duplicated. A claim older than the
    Celery hard time limit cannot belong to a live run (worker died, broker
    dropped the dispatch) and is treated as free, so the day never ends up
    stuck in ``processing``."""
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=CELERY_TASK_TIME_LIMIT)
    result = db.execute(
        text(
            """
            INSERT INTO home_briefings (user_id, day, status, queued_at)
            VALUES (:user_id, :day, 'processing', :now)
            ON CONFLICT (user_id, day) DO UPDATE
              SET status = 'processing', queued_at = :now, error = NULL
              WHERE home_briefings.queued_at IS NULL
                 OR home_briefings.queued_at < :stale_before
            """
        ),
        {"user_id": user_id, "day": day, "now": now, "stale_before": stale_before},
    )
    db.commit()
    return cast(CursorResult, result).rowcount == 1


def release_claim(db: Session, user_id: int, day: date) -> None:
    db.execute(
        text(
            "UPDATE home_briefings SET queued_at = NULL "
            "WHERE user_id = :user_id AND day = :day"
        ),
        {"user_id": user_id, "day": day},
    )
    db.commit()


def briefing_row(db: Session, user_id: int, day: date) -> HomeBriefing | None:
    return (
        db.query(HomeBriefing)
        .filter(HomeBriefing.user_id == user_id, HomeBriefing.day == day)
        .first()
    )


def _compose_prompt(data: dict, day: date, tz: tzinfo) -> str:
    """One sanitised line per item. Dates are the reader's calendar (``tz``)
    relative to the row's ``day``, so a retry after midnight still describes
    the day the row is stored under."""

    def local_date(due: datetime | None) -> date | None:
        return due.astimezone(tz).date() if due else None

    def days_until(due: date | None) -> str:
        if due is None:
            return "no date"
        delta = (due - day).days
        return f"{delta}d" if delta >= 0 else f"{-delta}d overdue"

    items = [
        f"- {sanitize_oneline(a.title, 120)} [{a.action_type}] case {a.case_id} "
        f"due {local_date(a.due_date) or 'unknown'} ({days_until(local_date(a.due_date))})"
        for a in data["today_items"][:12]
    ] or ["none"]
    triage = [
        f"- {sanitize_oneline(b.subject or b.sender_email, 100) or f'bundle #{b.id}'} "
        f"({len(b.documents)} docs)"
        for b in data["triage_bundles"][:8]
    ] or ["none"]
    delta = [
        f"- case {d['case_id']}: {d['new_doc_count']} new docs, "
        f"{d['new_actions']} new actions, max significance {d['max_significance']}"
        for d in data["delta_cases"][:8]
    ] or ["none"]
    signals = [
        f"- [{s['severity']}] {sanitize_oneline(s['title'], 100)}: "
        f"{sanitize_oneline(s['detail'], 160)}"
        for s in data["signals"][:6]
    ] or ["none"]
    activity = [
        f"- {e['kind']}: {sanitize_oneline(e['title'], 100)}"
        + (f" ({sanitize_oneline(e['detail'], 100)})" if e.get("detail") else "")
        for e in data["activity"][:8]
    ] or ["none"]
    nl = "\n"
    return (
        f"Today: {day.isoformat()}\n\n"
        f"Deadlines and hearings:\n{nl.join(items)}\n\n"
        f"Triage inbox:\n{nl.join(triage)}\n\n"
        f"New since last visit:\n{nl.join(delta)}\n\n"
        f"Signals:\n{nl.join(signals)}\n\n"
        f"Recent events:\n{nl.join(activity)}"
    )


def generate(user_id: int, day: date) -> None:
    """Compose today's input from the Home aggregation, ask the model, store
    the result on the (user, day) row. Raises on failure; the task decides
    whether to retry and marks the row failed on its terminal branch."""
    # Resolved at call time so the test suite's rebinding applies (#149).
    db: Session = config.SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            raise BriefingUserMissing(f"user {user_id} not found")
        cfg = get_chat_config(db)
        from app.services.ai_provider import chat_provider

        chat_provider.reload_from_db(db)

        data = HomeService(db).get_home_data(user_id)
        result = call_json_ai(
            system_prompt=HOME_BRIEFING_SYSTEM,
            user_prompt=_compose_prompt(data, day, get_user_tz()),
            options=STAGE_OPTIONS["home_briefing"],
            debug_label=f"user_{user_id}_briefing_{day.isoformat()}",
            schema=HomeBriefingOut,
            model=cfg.summary_model or None,
            db=db,
            two_pass=False,
        )
        row = briefing_row(db, user_id, day)
        if row is None:
            raise ValueError(f"briefing row for user {user_id} on {day} vanished")
        row.status = "ready"
        row.summary = result.summary.strip()
        row.priorities = list(
            dict.fromkeys(p.strip() for p in result.priorities if p.strip())
        )[:3]
        row.model_label = cfg.summary_model or None
        row.external = is_external_endpoint(cfg.base_url)
        row.error = None
        row.generated_at = datetime.now(UTC)
        db.commit()
        logger.info("Home briefing for user %s (%s) generated", user_id, day)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def mark_failed(user_id: int, day: date, error: str) -> None:
    db: Session = config.SessionLocal()
    try:
        row = briefing_row(db, user_id, day)
        if row is not None:
            row.status = "failed"
            row.error = error[:500]
            db.commit()
    finally:
        db.close()
