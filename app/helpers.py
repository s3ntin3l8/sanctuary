from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import false, or_
from sqlalchemy.orm import Session

from app.config import templates
from app.models.database import ActionItem, Case, CaseStatus, Document, IngestBatch
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    IngestBatchStatus,
)


def triage_inbox_count(db: Session, owner_id: int | None = None) -> int:
    """Bundles awaiting triage (plus loose pre-batch docs), optionally per owner.

    Counts IngestBatches rather than documents, to stay consistent with the
    feed which groups docs into bundles. Loose docs without a batch
    (historical pre-batch data) are counted individually as fallback.
    """
    batch_q = db.query(IngestBatch).filter(
        IngestBatch.status != IngestBatchStatus.COMPLETED,
        IngestBatch.status != IngestBatchStatus.AWAITING_SLICING,
    )
    loose_q = db.query(Document).filter(
        Document.ingest_batch_id.is_(None),
        or_(Document.case_id == "_TRIAGE", Document.needs_review.is_(True)),
    )
    if owner_id is not None:
        batch_q = batch_q.filter(IngestBatch.owner_id == owner_id)
        loose_q = loose_q.filter(Document.owner_id == owner_id)
    return batch_q.count() + loose_q.count()


def build_sidebar_counts(db: Session, owner_id: int | None = None) -> dict:
    """Computes sidebar badge counts. When ``owner_id`` is given, the triage and
    case counts are scoped to that user (per-user triage inbox + visible cases)."""
    triage_count = triage_inbox_count(db, owner_id)
    total_docs = db.query(Document).count()

    case_q = db.query(Case).filter(Case.status != CaseStatus.CLOSED)
    if owner_id is not None:
        from app.models.database import User
        from app.services import access_service

        visible = access_service.visible_case_ids(db, db.get(User, owner_id))
        if visible is not None:
            case_q = case_q.filter(Case.id.in_(visible))
    case_count = case_q.count()
    # Lazy import: app.api.worker_queue → app.api.__init__ pulls route modules
    # that import helpers, so a top-level import here would cycle.
    from app.api.worker_queue import compute_queue_counts

    queue_counts = compute_queue_counts(db, owner_id=owner_id)
    return {
        "triage_count": triage_count,
        "total_docs": total_docs,
        "case_count": case_count,
        "queue_depth_count": queue_counts["n_executing"] + queue_counts["n_queued"],
        "queue_failed_count": queue_counts["n_failed"],
    }


def render_page(
    request: Request,
    template_name: str,
    db: Session | None = None,
    **context,
):
    base_context = {}
    current_user = _current_user_for_template(request, db)
    if db is not None:
        notif_data = _build_notifications(db, user=current_user)
        counts = build_sidebar_counts(
            db, owner_id=current_user.id if current_user else None
        )
        counts["notification_count"] = notif_data["notification_count"]
        base_context["sidebar_counts"] = counts
        base_context.update(notif_data)
    base_context["current_user"] = current_user
    base_context.update(context)
    return templates.TemplateResponse(request, template_name, base_context)


def _current_user_for_template(request: Request, db: Session | None):
    """Resolve the current user for the base template context.

    Reuses the object a dependency already loaded (request.state.current_user)
    or loads it by the uid the auth gate validated. Returns None when there is
    no authenticated user (e.g. unauthenticated error pages).
    """
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached
    uid = getattr(request.state, "auth_user_id", None)
    if isinstance(uid, int) and db is not None:
        from app.models.database import User

        user = db.get(User, uid)
        if user is not None:
            request.state.current_user = user
            return user
    # Dev mode (AUTH_ENABLED=false): the gate sets no uid, so bind the
    # bootstrap admin lazily on this request's session.
    from app import config

    if not config.AUTH_ENABLED and db is not None:
        from app.services import auth_service

        user = auth_service.get_or_create_bootstrap_admin(db)
        db.commit()
        if user is not None:
            request.state.current_user = user
        return user
    return None


from app.models.database import (
    CostStatus,
    LegalCost,
)


def _build_notifications(db: Session, user=None) -> dict:
    """Build notification data for the header panel, scoped to the user.

    Deadlines/hearings/costs and case titles are limited to the user's visible
    cases (admins see all). Pending-docs are the per-user triage inbox (owned).
    """
    now = datetime.now(UTC)
    seven_days = timedelta(days=7)

    from app.services import access_service

    # None → unrestricted (admin); set → owned ∪ shared case ids; empty when no user.
    visible = access_service.visible_case_ids(db, user) if user is not None else set()

    def _case_scope(query, column):
        if visible is not None:
            return query.filter(column.in_(visible))
        return query

    overdue_deadlines = (
        _case_scope(
            db.query(ActionItem).filter(
                ActionItem.action_type == ActionItemType.DEADLINE,
                ActionItem.status == ActionItemStatus.OPEN,
                ActionItem.due_date < now,
            ),
            ActionItem.case_id,
        )
        .order_by(ActionItem.due_date.asc())
        .limit(5)
        .all()
    )
    upcoming_deadlines = (
        _case_scope(
            db.query(ActionItem).filter(
                ActionItem.action_type == ActionItemType.DEADLINE,
                ActionItem.status == ActionItemStatus.OPEN,
                ActionItem.due_date >= now,
                ActionItem.due_date <= now + seven_days,
            ),
            ActionItem.case_id,
        )
        .order_by(ActionItem.due_date.asc())
        .limit(5)
        .all()
    )
    upcoming_hearings = (
        _case_scope(
            db.query(ActionItem).filter(
                ActionItem.action_type == ActionItemType.COURT_DATE,
                ActionItem.due_date >= now,
                ActionItem.due_date <= now + seven_days,
            ),
            ActionItem.case_id,
        )
        .order_by(ActionItem.due_date.asc())
        .limit(5)
        .all()
    )
    # Pending docs = per-user triage inbox.
    pending_q = db.query(Document).filter(
        or_(Document.case_id == "_TRIAGE", Document.needs_review.is_(True))
    )
    if user is not None:
        pending_q = pending_q.filter(Document.owner_id == user.id)
    else:
        pending_q = pending_q.filter(false())
    pending_docs = pending_q.order_by(Document.ingest_date.desc()).limit(5).all()
    overdue_costs = (
        _case_scope(
            db.query(LegalCost).filter(
                LegalCost.due_at < now,
                LegalCost.status.notin_([CostStatus.BEZAHLT, CostStatus.ERSTATTET]),
            ),
            LegalCost.case_id,
        )
        .order_by(LegalCost.due_at.asc())
        .limit(5)
        .all()
    )

    cases_q = db.query(Case)
    if visible is not None:
        cases_q = cases_q.filter(Case.id.in_(visible))
    case_titles = {c.id: c.title for c in cases_q.all()}

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


def format_eur(value: float | None) -> str:
    """Formats a float as EUR with German-style punctuation: € 1.234,56"""
    if value is None:
        return "—"
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"€\u00a0{formatted}"
