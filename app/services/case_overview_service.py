"""Read model of a case for the dashboard shell (and the MCP ``get_case`` tool).

Side-effect free: nothing here writes to the database or the user's settings.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.helpers import build_cost_summary
from app.models.database import (
    ActionItem,
    Case,
    Document,
    IngestBatch,
    LegalCost,
    Proceeding,
    User,
)
from app.models.enums import ActionItemStatus, BriefState, ClaimStatus, CostStatus
from app.repositories.claim import ClaimRepository
from app.schemas.case_detail import (
    BriefStatus,
    BriefView,
    CaseActionItem,
    CaseDetail,
    CaseDocument,
    FinancialsSummary,
    PartyView,
    ProceedingView,
)
from app.services import access_service
from app.services.case_dashboard_service import _PARTY_ROLE_ORDER
from app.services.case_service import _compute_dormancy_alert
from app.services.triage_bundles import (
    actionable_review_reasons,
    summary_awaits_approval,
)


def proceeding_views(db: Session, case_id: str) -> list[ProceedingView]:
    rows = (
        db.query(Proceeding, func.count(Document.id))
        .outerjoin(Document, Document.proceeding_id == Proceeding.id)
        .filter(Proceeding.case_id == case_id)
        .group_by(Proceeding.id)
        .order_by(Proceeding.ingest_date.asc().nullslast(), Proceeding.id.asc())
        .all()
    )
    ids = [p.id for p, _ in rows]
    blockers: dict[int, int] = {}
    if len(rows) > 1:
        for model in (IngestBatch, ActionItem, LegalCost):
            for pid, n in (
                db.query(model.proceeding_id, func.count(model.id))
                .filter(model.proceeding_id.in_(ids))
                .group_by(model.proceeding_id)
                .all()
            ):
                if pid is not None:
                    blockers[pid] = blockers.get(pid, 0) + n
    return [
        ProceedingView(
            id=p.id,
            court_name=p.court_name,
            court_level=p.court_level,
            az_court=p.az_court,
            subject_matter=p.subject_matter,
            status=p.status,
            is_draft=bool(p.is_draft),
            doc_count=count,
            is_deletable=len(rows) > 1 and count == 0 and blockers.get(p.id, 0) == 0,
        )
        for p, count in rows
    ]


def case_financial_summary(case: Case, db: Session) -> FinancialsSummary:
    costs = db.query(LegalCost).filter(LegalCost.case_id == case.id).all()
    return summary_of(costs, case.total_cost_exposure or 0)


def summary_of(costs: list[LegalCost], exposure_cents: int) -> FinancialsSummary:
    s = build_cost_summary(costs, CostStatus)
    return FinancialsSummary(
        total_cost_exposure_cents=exposure_cents,
        booked=s["total_gross"],
        paid=s["total_paid"],
        outstanding=s["total_outstanding"],
        reimbursable=s["total_reimbursable"],
    )


def action_item_view(a: ActionItem, today) -> CaseActionItem:
    return CaseActionItem(
        id=a.id,
        title=a.title,
        description=a.description,
        due_date=a.due_date,
        action_type=a.action_type,
        status=a.status,
        location=a.location,
        addressee=a.addressee,
        proceeding_id=a.proceeding_id,
        source_document_id=a.source_document_id,
        is_overdue=a.status == ActionItemStatus.OPEN
        and a.due_date is not None
        and a.due_date < today,
    )


def serialize_brief(case: Case) -> BriefView:
    """The last good brief plus the job state of its regeneration."""
    raw = case.ai_brief or {}
    status: BriefStatus
    if case.brief_state == BriefState.PROCESSING:
        status = "processing"
    elif case.brief_state == BriefState.FAILED:
        status = "failed"
    else:
        status = "ready" if raw else "none"
    return BriefView(
        status=status,
        error=case.brief_error if status == "failed" else None,
        posture=raw.get("posture"),
        pressure_points=[str(p) for p in raw.get("pressure_points") or []],
        next_move=raw.get("next_move"),
        detected_status=raw.get("detected_status"),
        status_rationale=raw.get("status_rationale"),
        updated_at=case.ai_brief_updated_at if raw else None,
    )


def build_case_detail(
    db: Session,
    user: User,
    case: Case,
    *,
    active_proceeding_id: int | None,
    last_visit: datetime | None,
    proceedings: list[ProceedingView] | None = None,
) -> CaseDetail:
    """The case overview scoped to ``active_proceeding_id`` (None = all proceedings).

    ``proceedings`` may be passed when the caller already loaded them.
    """
    procs = proceedings if proceedings is not None else proceeding_views(db, case.id)
    active = active_proceeding_id
    docs_q = db.query(Document).filter(Document.case_id == case.id)
    if active is not None:
        docs_q = docs_q.filter(Document.proceeding_id == active)
    docs = docs_q.order_by(
        Document.issued_date.desc().nullslast(), Document.id.desc()
    ).all()
    documents = [
        CaseDocument(
            id=d.id,
            title=d.title,
            originator_type=d.originator_type,
            document_type=d.document_type,
            issued_date=d.issued_date,
            received_date=d.received_date,
            significance_tier=d.significance_tier,
            role=d.role.value,
            thread_open=bool(d.thread_open),
            needs_review=bool(d.needs_review),
            open_review_reasons=actionable_review_reasons(d.review_reasons),
            summary_pending=summary_awaits_approval(d),
            is_new=bool(last_visit and d.ingest_date and d.ingest_date > last_visit),
        )
        for d in docs
    ]
    today = now_utc()
    items = (
        db.query(ActionItem)
        .filter(ActionItem.case_id == case.id, ActionItem.superseded.is_(False))
        .order_by(ActionItem.due_date.asc().nullslast(), ActionItem.id.asc())
        .all()
    )
    open_claims = len(
        ClaimRepository(db).claims_for_case(
            case.id,
            statuses=[
                ClaimStatus.CONTESTED,
                ClaimStatus.ASSERTED,
                ClaimStatus.NEEDS_PROOF,
            ],
        )
    )
    detail = CaseDetail(
        id=case.id,
        title=case.title,
        status=case.status,
        case_type=case.case_type,
        jurisdiction=case.jurisdiction.value,
        is_draft=bool(case.is_draft),
        pending_close=bool(case.pending_close),
        close_suggestion_rationale=(case.ai_brief or {}).get(
            "close_suggestion_rationale"
        ),
        assume_worst_case=bool(case.assume_worst_case),
        can_edit=access_service.can_edit_case(db, user, case),
        can_manage_sharing=access_service.is_admin(user) or case.owner_id == user.id,
        proceedings=procs,
        active_proceeding_id=active,
        documents=documents,
        new_doc_count=sum(1 for d in documents if d.is_new),
        last_visit=last_visit,
        action_items=[action_item_view(a, today) for a in items],
        parties=[
            PartyView(
                name=str(p.get("name", "")),
                role=str(p.get("role", "unknown")),
                document_count=int(p.get("document_count") or 0),
            )
            for p in sorted(
                case.parties or [],
                key=lambda p: _PARTY_ROLE_ORDER.get(p.get("role", "unknown"), 99),
            )
        ],
        opposing_parties=list(case.opposing_parties or []),
        brief=serialize_brief(case),
        financials=case_financial_summary(case, db),
        open_claim_count=int(open_claims),
        dormancy_alert=_compute_dormancy_alert(case, db),
    )
    return detail
