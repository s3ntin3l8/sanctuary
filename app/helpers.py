from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import templates
from app.models.database import ActionItem, Case, CaseStatus, Document, IngestBatch
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    IngestBatchStatus,
)
from app.services.ai_inflight import count_inflight


def build_sidebar_counts(db: Session) -> dict:
    """Computes sidebar badge counts using the active request session."""
    # Count bundles (IngestBatches) pending triage rather than documents, to stay
    # consistent with the feed which groups docs into bundles. Loose docs without a
    # batch (historical pre-batch data) are counted individually as fallback.
    batch_count = (
        db.query(IngestBatch)
        .filter(
            IngestBatch.status != IngestBatchStatus.COMPLETED,
            IngestBatch.status != IngestBatchStatus.AWAITING_SLICING,
        )
        .count()
    )
    loose_count = (
        db.query(Document)
        .filter(
            Document.ingest_batch_id.is_(None),
            or_(Document.case_id == "_TRIAGE", Document.needs_review),
        )
        .count()
    )
    triage_count = batch_count + loose_count
    total_docs = db.query(Document).count()
    case_count = db.query(Case).filter(Case.status != CaseStatus.CLOSED).count()
    return {
        "triage_count": triage_count,
        "total_docs": total_docs,
        "case_count": case_count,
        "ai_inflight_count": count_inflight(),
    }


def render_page(
    request: Request,
    template_name: str,
    db: Session | None = None,
    **context,
):
    base_context = {}
    if db is not None:
        notif_data = _build_notifications(db)
        counts = build_sidebar_counts(db)
        counts["notification_count"] = notif_data["notification_count"]
        base_context["sidebar_counts"] = counts
        base_context.update(notif_data)
    base_context.update(context)
    return templates.TemplateResponse(request, template_name, base_context)


from app.models.database import (
    CostStatus,
    LegalCost,
)


def _build_notifications(db: Session) -> dict:
    """Build notification data for the header notifications panel."""
    now = datetime.now(UTC)
    seven_days = timedelta(days=7)

    overdue_deadlines = (
        db.query(ActionItem)
        .filter(
            ActionItem.action_type == ActionItemType.DEADLINE,
            ActionItem.status == ActionItemStatus.OPEN,
            ActionItem.due_date < now,
        )
        .order_by(ActionItem.due_date.asc())
        .limit(5)
        .all()
    )
    upcoming_deadlines = (
        db.query(ActionItem)
        .filter(
            ActionItem.action_type == ActionItemType.DEADLINE,
            ActionItem.status == ActionItemStatus.OPEN,
            ActionItem.due_date >= now,
            ActionItem.due_date <= now + seven_days,
        )
        .order_by(ActionItem.due_date.asc())
        .limit(5)
        .all()
    )
    upcoming_hearings = (
        db.query(ActionItem)
        .filter(
            ActionItem.action_type == ActionItemType.COURT_DATE,
            ActionItem.due_date >= now,
            ActionItem.due_date <= now + seven_days,
        )
        .order_by(ActionItem.due_date.asc())
        .limit(5)
        .all()
    )
    pending_docs = (
        db.query(Document)
        .filter(or_(Document.case_id == "_TRIAGE", Document.needs_review))
        .order_by(Document.ingest_date.desc())
        .limit(5)
        .all()
    )
    overdue_costs = (
        db.query(LegalCost)
        .filter(
            LegalCost.due_at < now,
            LegalCost.status.notin_([CostStatus.BEZAHLT, CostStatus.ERSTATTET]),
        )
        .order_by(LegalCost.due_at.asc())
        .limit(5)
        .all()
    )

    all_cases = db.query(Case).all()
    case_titles = {c.id: c.title for c in all_cases}

    notification_count = (
        len(overdue_deadlines)
        + len(upcoming_deadlines)
        + len(upcoming_hearings)
        + len(pending_docs)
        + len(overdue_costs)
    )

    return {
        "notification_count": notification_count,
        "overdue_deadlines": overdue_deadlines,
        "upcoming_deadlines_notif": upcoming_deadlines,
        "upcoming_hearings_notif": upcoming_hearings,
        "pending_docs": pending_docs,
        "overdue_costs_notif": overdue_costs,
        "case_titles": case_titles,
    }


def format_due_relative(value) -> str:
    """Return a compact due-date label that handles both future and past dates.

    Future: 'today', 'in 3d', 'in 2w'
    Past:   'overdue 2d', 'overdue 3w'
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    if value is None:
        return "—"
    d = value.date() if hasattr(value, "date") else value
    today = _dt.now(tz=_UTC).date()
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta > 0:
        if delta <= 14:
            return f"in {delta}d"
        return f"in {delta // 7}w"
    # overdue
    overdue = -delta
    if overdue <= 14:
        return f"overdue {overdue}d"
    return f"overdue {overdue // 7}w"


def format_relative_time(value: datetime) -> str:
    """Returns a compact human-readable relative timestamp."""
    if value is None:
        return "unknown"
    now = datetime.now(UTC)
    if value.tzinfo is None:
        # If DB handed us naive, treat as UTC
        value = value.replace(tzinfo=UTC)
    delta = now - value
    total_seconds = max(int(delta.total_seconds()), 0)
    if total_seconds < 60:
        return "just now"
    if total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes}m ago"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        return f"{hours}h ago"
    days = total_seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    from app.services.timezone_service import get_user_tz

    return value.astimezone(get_user_tz()).strftime("%b %d, %Y")


def format_days_ago(value: datetime) -> str:
    """Always-relative compact label used in the timeline right column.

    Past:   'Xd ago', 'Xmo ago', 'Xy ago'
    Future: 'in Xd', 'in Xmo'
    Today:  'today'
    """
    if value is None:
        return "—"
    now = datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = now - value
    days = int(delta.total_seconds() / 86400)
    if abs(days) == 0:
        return "today"
    if days > 0:
        if days < 60:
            return f"{days}d ago"
        if days < 365:
            return f"{days // 30}mo ago"
        years = days // 365
        rem = (days % 365) // 30
        return f"{years}y {rem}mo ago" if rem else f"{years}y ago"
    # future
    future_days = -days
    if future_days < 60:
        return f"in {future_days}d"
    if future_days < 365:
        return f"in {future_days // 30}mo"
    return f"in {future_days // 365}y"


def format_upcoming_datetime(value: datetime) -> str:
    """Formats upcoming deadlines/hearings for compact dashboard display."""
    from app.services.timezone_service import get_user_tz

    tz = get_user_tz()
    now = datetime.now(tz)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local_value = value.astimezone(tz)
    delta_days = (local_value.date() - now.date()).days
    if delta_days == 0:
        day_label = "Today"
    elif delta_days == 1:
        day_label = "Tomorrow"
    else:
        day_label = local_value.strftime("%a, %b %d")
    return f"{day_label} at {local_value.strftime('%H:%M')}"


def format_deadline_badge(value: datetime) -> dict:
    """Returns a compact urgency label + tone for dashboard deadline cards."""
    now = datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    day_delta = (value.date() - now.date()).days
    if day_delta < 0:
        return {"label": "Overdue", "tone": "bg-error-container/30 text-error"}
    if day_delta == 0:
        return {"label": "Today", "tone": "bg-error-container/30 text-error"}
    if day_delta == 1:
        return {
            "label": "1 day left",
            "tone": "bg-originator-opposing/10 text-originator-opposing",
        }
    if day_delta < 7:
        return {
            "label": f"{day_delta} days left",
            "tone": "bg-originator-opposing/10 text-originator-opposing",
        }
    from app.services.timezone_service import get_user_tz

    return {
        "label": value.astimezone(get_user_tz()).strftime("%b %d"),
        "tone": "bg-surface-container-high text-on-surface-variant",
    }


def format_form_datetime(value: datetime | None) -> str:
    """Formats datetimes for datetime-local form fields."""
    if value is None:
        return ""
    return value.strftime("%Y-%m-%dT%H:%M")


def parse_form_datetime(raw_value: str | None) -> datetime | None:
    """Parses datetime-local input values, tolerating blanks."""
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def build_cost_summary(costs: list, CostStatus) -> dict:
    total_gross = sum(c.amount_gross or 0 for c in costs)
    total_paid = sum(c.amount_paid or 0 for c in costs)
    total_reimbursed = sum(c.amount_reimbursed or 0 for c in costs)
    total_outstanding = sum(
        (c.amount_gross or 0) - (c.amount_paid or 0)
        for c in costs
        if c.status not in (CostStatus.BEZAHLT, CostStatus.ERSTATTET)
    )
    total_reimbursable = sum(
        c.amount_gross - c.amount_reimbursed
        for c in costs
        if c.is_reimbursable and c.status not in (CostStatus.ERSTATTET,)
    )
    return {
        "total_gross": total_gross,
        "total_paid": total_paid,
        "total_reimbursed": total_reimbursed,
        "total_outstanding": total_outstanding,
        "total_reimbursable": total_reimbursable,
    }


def toast_trigger(
    message: str,
    type: str = "info",
    action: dict | None = None,
) -> dict:
    """Build the HX-Trigger payload for a server-pushed toast.

    Routes that want to flash a success/info/warning toast set::

        response.headers["HX-Trigger"] = json.dumps(toast_trigger("Saved", "success"))

    HTMX dispatches `showToast` as a CustomEvent on the body when the header
    arrives; the listener in base.html calls window.showToast(message, type, action).

    `action`, when provided, is a `{"href": ..., "label": ...}` dict that
    renders a small click-through link in the toast (destination feedback).
    """
    payload: dict = {"message": message, "type": type}
    if action:
        payload["action"] = action
    return {"showToast": payload}


def format_eur(value: float | None) -> str:
    """Formats a float as EUR with German-style punctuation: € 1.234,56"""
    if value is None:
        return "—"
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€\u00a0{formatted}"
