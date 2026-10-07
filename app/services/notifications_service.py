"""What needs the caller right now, grouped for the rail's 🔔 panel.

Five groups, scoped to the cases the caller may see (owned ∪ shared, or
everything for admins) and to the caller's own triage inbox:

* overdue deadlines (any type but hearings, due before now)
* deadlines due within seven days
* hearings within thirty days, including ones already missed
* the caller's bundles awaiting triage
* overdue costs (not paid or reimbursed, due before now)

Each group carries its full count and at most ``ITEMS_PER_GROUP`` rows;
``total`` is the badge number.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.core.timezone import ensure_utc, now_utc
from app.models.database import ActionItem, IngestBatch, LegalCost, User
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    IngestBatchStatus,
)
from app.schemas.notifications import (
    NotificationGroup,
    NotificationItem,
    NotificationKind,
    NotificationsView,
)
from app.services import access_service
from app.services.cost_service import costs_due

ITEMS_PER_GROUP = 5
DEADLINE_WINDOW = timedelta(days=7)
HEARING_WINDOW = timedelta(days=30)


def build_notifications(db: Session, user: User) -> NotificationsView:
    now = now_utc()
    visible = access_service.visible_case_ids(db, user)

    items_q = (
        db.query(ActionItem)
        .options(joinedload(ActionItem.case))
        .filter(
            ActionItem.status == ActionItemStatus.OPEN,
            ActionItem.superseded.is_(False),
            # Same rule as Home: the caller's own obligations only.
            or_(ActionItem.addressee == "user", ActionItem.addressee.is_(None)),
            ActionItem.due_date <= now + HEARING_WINDOW,
        )
    )
    if visible is not None:
        items_q = items_q.filter(ActionItem.case_id.in_(visible))
    action_items = sorted(items_q.all(), key=lambda a: ensure_utc(a.due_date))

    overdue: list[NotificationItem] = []
    upcoming: list[NotificationItem] = []
    hearings: list[NotificationItem] = []
    for a in action_items:
        due = ensure_utc(a.due_date)
        if a.action_type == ActionItemType.COURT_DATE:
            hearings.append(_action_row(a, "hearing"))
        elif due < now:
            overdue.append(_action_row(a, "overdue_deadline"))
        elif due <= now + DEADLINE_WINDOW:
            upcoming.append(_action_row(a, "upcoming_deadline"))

    batches = (
        db.query(IngestBatch)
        .filter(
            IngestBatch.owner_id == user.id,
            IngestBatch.status.notin_(
                (IngestBatchStatus.COMPLETED, IngestBatchStatus.AWAITING_SLICING)
            ),
        )
        .order_by(IngestBatch.received_at.desc())
        .all()
    )
    pending = [
        NotificationItem(
            id=b.id,
            kind="pending_triage",
            title=b.subject or b.sender_email or f"Bundle #{b.id}",
            detail=b.sender_email if b.subject else None,
            due_at=b.received_at,
            case_id=None,
            case_title=None,
            link="/triage",
        )
        for b in batches
    ]

    costs_q = db.query(LegalCost).options(joinedload(LegalCost.case))
    if visible is not None:
        costs_q = costs_q.filter(LegalCost.case_id.in_(visible))
    overdue_costs = [
        NotificationItem(
            id=d.cost.id,
            kind="overdue_cost",
            title=d.cost.title,
            detail=f"{d.open_amount:,.2f} € open",
            due_at=d.cost.due_at,
            case_id=d.cost.case_id,
            case_title=d.cost.case.title if d.cost.case else None,
            link="/costs",
        )
        for d in costs_due(costs_q.all(), now, within_days=0)
        if d.overdue
    ]

    groups = [
        _group("overdue_deadline", overdue),
        _group("upcoming_deadline", upcoming),
        _group("hearing", hearings),
        _group("pending_triage", pending),
        _group("overdue_cost", overdue_costs),
    ]
    return NotificationsView(
        total=sum(g.count for g in groups), groups=groups, generated_at=now
    )


def _action_row(a: ActionItem, kind: NotificationKind) -> NotificationItem:
    return NotificationItem(
        id=a.id,
        kind=kind,
        title=a.title,
        detail=a.location if kind == "hearing" else a.description,
        due_at=a.due_date,
        case_id=a.case_id,
        case_title=a.case.title if a.case else None,
        link=f"/cases/{a.case_id}?view=review",
    )


def _group(kind: NotificationKind, items: list[NotificationItem]) -> NotificationGroup:
    return NotificationGroup(kind=kind, count=len(items), items=items[:ITEMS_PER_GROUP])
