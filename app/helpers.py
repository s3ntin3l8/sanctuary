from datetime import datetime
from typing import Optional
from fastapi import Request
from sqlalchemy.orm import Session

from app.config import templates
from app.models.database import Case, CaseStatus, Deadline, Document, Hearing


def build_sidebar_counts(db: Session) -> dict:
    """Computes sidebar badge counts using the active request session."""
    triage_count = db.query(Document).filter(Document.needs_review == True).count()
    total_docs = db.query(Document).count()
    case_count = db.query(Case).filter(Case.status != CaseStatus.CLOSED).count()
    return {
        "triage_count": triage_count,
        "total_docs": total_docs,
        "case_count": case_count,
    }


def render_page(
    request: Request,
    template_name: str,
    db: Optional[Session] = None,
    **context,
):
    base_context = {"request": request}
    if db is not None:
        base_context["sidebar_counts"] = build_sidebar_counts(db)
        base_context.update(_build_notifications(db))
    base_context.update(context)
    return templates.TemplateResponse(template_name, base_context)


from app.models.database import (
    Case,
    CaseStatus,
    CostStatus,
    Deadline,
    Document,
    Hearing,
    LegalCost,
)
from datetime import timedelta


def _build_notifications(db: Session) -> dict:
    """Build notification data for the header notifications panel."""
    now = datetime.utcnow()
    seven_days = timedelta(days=7)

    overdue_deadlines = (
        db.query(Deadline)
        .filter(Deadline.completed == False, Deadline.due_at < now)
        .order_by(Deadline.due_at.asc())
        .limit(5)
        .all()
    )
    upcoming_deadlines = (
        db.query(Deadline)
        .filter(
            Deadline.completed == False,
            Deadline.due_at >= now,
            Deadline.due_at <= now + seven_days,
        )
        .order_by(Deadline.due_at.asc())
        .limit(5)
        .all()
    )
    upcoming_hearings = (
        db.query(Hearing)
        .filter(Hearing.scheduled_for >= now, Hearing.scheduled_for <= now + seven_days)
        .order_by(Hearing.scheduled_for.asc())
        .limit(5)
        .all()
    )
    pending_docs = (
        db.query(Document)
        .filter(Document.needs_review == True)
        .order_by(Document.created_at.desc())
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


def format_relative_time(value: datetime) -> str:
    """Returns a compact human-readable relative timestamp."""
    delta = datetime.utcnow() - value
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
    return value.strftime("%b %d, %Y")


def format_upcoming_datetime(value: datetime) -> str:
    """Formats upcoming deadlines/hearings for compact dashboard display."""
    delta_days = (value.date() - datetime.utcnow().date()).days
    if delta_days == 0:
        day_label = "Today"
    elif delta_days == 1:
        day_label = "Tomorrow"
    else:
        day_label = value.strftime("%a, %b %d")
    return f"{day_label} at {value.strftime('%H:%M')}"


def format_deadline_badge(value: datetime) -> dict:
    """Returns a compact urgency label + tone for dashboard deadline cards."""
    day_delta = (value.date() - datetime.utcnow().date()).days
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
    return {
        "label": value.strftime("%b %d"),
        "tone": "bg-surface-container-high text-on-surface-variant",
    }


def format_form_datetime(value: Optional[datetime]) -> str:
    """Formats datetimes for datetime-local form fields."""
    if value is None:
        return ""
    return value.strftime("%Y-%m-%dT%H:%M")


def parse_form_datetime(raw_value: Optional[str]) -> Optional[datetime]:
    """Parses datetime-local input values, tolerating blanks."""
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def load_case_schedule(db: Session, case_id: str) -> dict:
    """Loads schedule data for the case calendar panel."""
    now = datetime.utcnow()
    deadlines = (
        db.query(Deadline)
        .filter(Deadline.case_id == case_id)
        .order_by(Deadline.completed.asc(), Deadline.due_at.asc())
        .all()
    )
    hearings = (
        db.query(Hearing)
        .filter(Hearing.case_id == case_id)
        .order_by(Hearing.scheduled_for.asc())
        .all()
    )
    return {
        "upcoming_deadlines": [
            item for item in deadlines if not item.completed and item.due_at >= now
        ],
        "completed_deadlines": [
            item for item in deadlines if item.completed or item.due_at < now
        ],
        "upcoming_hearings": [item for item in hearings if item.scheduled_for >= now],
        "past_hearings": [item for item in hearings if item.scheduled_for < now],
    }


def render_case_schedule_panel(
    request: Request,
    db: Session,
    case_id: str,
    deadline_errors=None,
    deadline_data=None,
    hearing_errors=None,
    hearing_data=None,
):
    schedule = load_case_schedule(db, case_id)
    return render_page(
        request,
        "partials/case_schedule_panel.html",
        db=db,
        case_id=case_id,
        format_upcoming_datetime=format_upcoming_datetime,
        format_form_datetime=format_form_datetime,
        deadline_errors=deadline_errors or [],
        deadline_data=deadline_data or {},
        hearing_errors=hearing_errors or [],
        hearing_data=hearing_data or {},
        **schedule,
    )


def build_document_extraction_context(db: Session, doc: Optional[Document]) -> dict:
    """Builds extracted schedule candidates and already-promoted records for a document."""
    if not doc:
        return {
            "schedule_candidates": [],
            "linked_deadlines": [],
            "linked_hearings": [],
        }

    from app.services.ingestion import extract_schedule_candidates

    schedule_candidates = extract_schedule_candidates(
        doc.content or "", base_date=doc.received_date
    )
    linked_deadlines = (
        db.query(Deadline)
        .filter(Deadline.source_document_id == doc.id)
        .order_by(Deadline.due_at.asc())
        .all()
    )
    linked_hearings = (
        db.query(Hearing)
        .filter(Hearing.source_document_id == doc.id)
        .order_by(Hearing.scheduled_for.asc())
        .all()
    )
    return {
        "schedule_candidates": schedule_candidates,
        "linked_deadlines": linked_deadlines,
        "linked_hearings": linked_hearings,
    }


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


def format_eur(value: Optional[float]) -> str:
    """Formats a float as EUR with German-style punctuation: € 1.234,56"""
    if value is None:
        return "—"
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€\u00a0{formatted}"
