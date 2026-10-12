"""Read model of a case for the dashboard shell (and the MCP ``get_case`` tool).

Side-effect free: nothing here writes to the database or the user's settings.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.timezone import now_utc
from app.helpers import build_cost_summary
from app.models.database import (
    ActionItem,
    Case,
    CostSignal,
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
    CostRow,
    CostSignalDoc,
    FinancialsSummary,
    FinancialsView,
    InstanceExposure,
    PartyView,
    ProceedingView,
)
from app.services import access_service
from app.services.case_dashboard_service import _PARTY_ROLE_ORDER
from app.services.case_service import (
    _compute_dormancy_alert,
    build_case_level_costs,
    build_proceeding_exposure,
)
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


def cost_row(c: LegalCost) -> CostRow:
    return CostRow(
        id=c.id,
        case_id=c.case_id,
        proceeding_id=c.proceeding_id,
        title=c.title,
        category=c.category,
        status=c.status,
        rvg_position=c.rvg_position,
        amount_net=c.amount_net,
        vat_rate=c.vat_rate,
        amount_gross=c.amount_gross,
        amount_paid=c.amount_paid,
        amount_reimbursed=c.amount_reimbursed,
        is_reimbursable=c.is_reimbursable,
        issued_at=c.issued_at,
        due_at=c.due_at,
        paid_at=c.paid_at,
        streitwert=c.streitwert,
        gebuehren_faktor=c.gebuehren_faktor,
        notes=c.notes,
        auto_created=bool(c.auto_created),
        source_document_id=c.source_document_id,
    )


def build_financials(db: Session, case: Case) -> FinancialsView:
    instances = []
    for row in build_proceeding_exposure(case.id, db):
        proc = row["proceeding"]
        instances.append(
            InstanceExposure(
                proceeding_id=proc.id,
                court_name=proc.court_name,
                court_level=proc.court_level,
                az_court=proc.az_court,
                streitwert=row.get("streitwert"),
                allocation_source=row.get("allocation_source") or "",
                own_lawyer_gross=row["own_lawyer_gross"],
                own_lawyer_source=row["own_lawyer_source"],
                court_fee=row["court_fee"],
                court_fee_share=row["court_fee_share"],
                court_fee_charged=row["court_fee_charged"],
                court_fee_source=row["court_fee_source"],
                opposing_gross=row["opposing_gross"],
                opposing_source=row["opposing_source"],
                subtotal=row["subtotal"],
                own_theoretical=row["own_theoretical"],
                court_theoretical=row["court_theoretical"],
                opposing_theoretical=row["opposing_theoretical"],
                invoices_own=[cost_row(c) for c in row["invoices_own"]],
                invoices_court=[cost_row(c) for c in row["invoices_court"]],
                invoices_opposing=[cost_row(c) for c in row["invoices_opposing"]],
                invoices_other=[cost_row(c) for c in row["invoices_other"]],
            )
        )
    signals = (
        db.query(CostSignal)
        .options(joinedload(CostSignal.source_document))
        .filter(CostSignal.case_id == case.id)
        .order_by(CostSignal.issued_at.desc().nullslast(), CostSignal.id.desc())
        .all()
    )
    signal_docs = [
        CostSignalDoc(
            id=s.source_document.id,
            title=s.source_document.title,
            issued_date=s.source_document.issued_date,
            originator_type=s.source_document.originator_type,
            signal_type=s.signal_type,
            signal_id=s.id,
            amount=s.amount,
            description=s.description,
            client_role=(s.allocation or {}).get("client_role"),
            role_source="auto" if (s.allocation or {}).get("auto_detected") else None,
        )
        for s in signals
        if s.source_document is not None
    ]
    return FinancialsView(
        summary=case_financial_summary(case, db),
        instances=instances,
        case_level_costs=[cost_row(c) for c in build_case_level_costs(case.id, db)],
        signal_docs=signal_docs,
    )
