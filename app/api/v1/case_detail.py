"""The case dashboard: detail, brief, graph, timeline, financials, sharing,
and the case/proceeding mutations the dashboard offers."""

from __future__ import annotations

import dataclasses
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.access_guards import (
    require_action_item_access,
    require_case_access,
    require_proceeding_access,
)
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.dependencies import get_current_user, get_db
from app.helpers import build_cost_summary
from app.models.database import (
    ActionItem,
    Case,
    CaseShare,
    CostSignal,
    Document,
    IngestBatch,
    LegalCost,
    Proceeding,
    User,
)
from app.models.enums import (
    ActionItemStatus,
    BriefState,
    CaseStatus,
    ClaimStatus,
    CostStatus,
    ProceedingStatus,
)
from app.repositories.claim import ClaimRepository
from app.repositories.proceeding import ProceedingRepository
from app.schemas.case_detail import (
    BriefStatus,
    BriefView,
    CaseActionItem,
    CaseDetail,
    CaseDocument,
    CasePurge,
    CaseUpdate,
    CostRow,
    CostSignalDoc,
    FinancialsSummary,
    FinancialsView,
    GraphView,
    InstanceExposure,
    OpposingPartiesUpdate,
    PartyView,
    ProceedingUpdate,
    ProceedingView,
    ReenrichResult,
    ShareCreate,
    ShareView,
    SharingView,
    SignificanceFilter,
    TimelineEventView,
    TimelineView,
)
from app.schemas.document_review import ActionStatusUpdate
from app.services import access_service, auth_service, user_settings_service
from app.services.case_dashboard_service import (
    _PARTY_ROLE_ORDER,
    reaction_map_for_proceeding,
)
from app.services.case_graph_service import CaseGraphService
from app.services.case_service import (
    CaseService,
    _compute_dormancy_alert,
    build_case_level_costs,
    build_proceeding_exposure,
    set_case_opposing_parties,
)
from app.services.case_timeline_service import CaseTimelineService

router = APIRouter(tags=["cases"])


# --- Helpers -----------------------------------------------------------------


def _proceedings(db: Session, case_id: str) -> list[ProceedingView]:
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


def _resolve_active(
    db: Session,
    user: User,
    case_id: str,
    procs: list[ProceedingView],
    requested: int | None,
) -> int | None:
    ids = {p.id for p in procs}
    if requested is not None and requested in ids:
        user_settings_service.set_active_proceeding(case_id, requested, db, user.id)
        db.commit()
        return requested
    saved = user_settings_service.get_active_proceeding(case_id, db, user.id)
    if saved in ids:
        return saved
    return procs[0].id if procs else None


def _summary(case: Case, db: Session) -> FinancialsSummary:
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


def _cost_row(c: LegalCost) -> CostRow:
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


def _action_item(a: ActionItem, today) -> CaseActionItem:
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


# --- Detail ------------------------------------------------------------------


@router.get("/cases/{case_id}", response_model=CaseDetail)
def case_detail(
    proceeding: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access()),
):
    """Everything the dashboard shell needs; marks the case as viewed."""
    procs = _proceedings(db, case.id)
    active = _resolve_active(db, user, case.id, procs, proceeding)
    last_visit = user_settings_service.get_last_viewed(case.id, db, user.id)

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
        action_items=[_action_item(a, today) for a in items],
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
        brief=_serialize_brief(case),
        financials=_summary(case, db),
        open_claim_count=int(open_claims),
        dormancy_alert=_compute_dormancy_alert(case, db),
    )
    return detail


@router.post("/cases/{case_id}/viewed", status_code=204, response_class=Response)
def mark_case_viewed(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access()),
):
    """Record the visit once the dashboard has rendered its "new since" markers."""
    user_settings_service.mark_viewed(case.id, db, user.id)
    db.commit()


@router.patch("/cases/{case_id}", response_model=CaseDetail)
def update_case(
    body: CaseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access(edit=True)),
):
    if body.title is not None:
        case.title = body.title
    if body.status is not None:
        case.status = body.status
        if body.status == CaseStatus.CLOSED:
            case.closed_at = now_utc()
            case.pending_close = False
            db.query(Proceeding).filter(Proceeding.case_id == case.id).update(
                {"status": ProceedingStatus.CLOSED}
            )
        else:
            case.closed_at = None
    if body.case_type is not None:
        case.case_type = body.case_type
    if body.assume_worst_case is not None:
        case.assume_worst_case = body.assume_worst_case
    db.commit()
    return case_detail(None, db, user, case)


@router.post("/cases/{case_id}/purge", status_code=204, response_class=Response)
@limiter.limit("5/minute")
def purge_case(
    request: Request,
    body: CasePurge,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Hard-delete a case and erase its on-disk data directory."""
    expected = f"purge {case.id}"
    if body.confirm != expected:
        raise ApiError(422, "confirm_mismatch", f"Type '{expected}' to confirm.")
    try:
        result = CaseService(db).purge(case.id)
    except ValueError as e:
        raise ApiError(409, "purge_refused", str(e)) from e
    if result is None:
        raise ApiError(404, "not_found", "Case not found.")


@router.put("/cases/{case_id}/opposing-parties", response_model=list[PartyView])
def save_opposing_parties(
    body: OpposingPartiesUpdate,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    set_case_opposing_parties(case.id, body.opposing_parties, db)
    db.commit()
    db.refresh(case)
    return [
        PartyView(
            name=str(p.get("name", "")),
            role=str(p.get("role", "unknown")),
            document_count=int(p.get("document_count") or 0),
        )
        for p in case.parties or []
    ]


@router.post("/cases/{case_id}/reenrich", response_model=ReenrichResult)
@limiter.limit("5/minute")
def reenrich_case(
    request: Request,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Queue every document of the case for re-enrichment with the current party identity."""
    from app.services.triage_confirmation import reset_and_reenrich

    docs = db.query(Document).filter(Document.case_id == case.id).all()
    if docs:
        reset_and_reenrich(db, docs)
    return ReenrichResult(queued=len(docs))


@router.patch("/action-items/{item_id}", response_model=CaseActionItem)
def set_action_item_status(
    body: ActionStatusUpdate,
    db: Session = Depends(get_db),
    item: ActionItem = Depends(require_action_item_access(edit=True)),
):
    item.status = body.status
    db.commit()
    db.refresh(item)
    return _action_item(item, now_utc())


# --- Brief -------------------------------------------------------------------


def _serialize_brief(case: Case) -> BriefView:
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


@router.get("/cases/{case_id}/brief", response_model=BriefView)
def get_brief(case: Case = Depends(require_case_access())):
    return _serialize_brief(case)


@router.post("/cases/{case_id}/brief/refresh", response_model=BriefView)
@limiter.limit("10/minute")
def refresh_brief(
    request: Request,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    from app.tasks.dispatch import dispatch_task
    from app.tasks.generate_case_brief import refresh_case_brief_task

    case.brief_state = BriefState.PROCESSING
    case.brief_error = None
    db.commit()
    dispatch_task(refresh_case_brief_task, case.id)
    return _serialize_brief(case)


# --- Graph / timeline / financials -----------------------------------------


@router.get("/cases/{case_id}/graph", response_model=GraphView)
def case_graph(
    proceeding: int,
    filter: SignificanceFilter = Query("significant+"),
    since: datetime | None = Query(
        None, description="The visit timestamp the detail call returned as last_visit"
    ),
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access()),
):
    if (
        not db.query(Proceeding.id)
        .filter(Proceeding.id == proceeding, Proceeding.case_id == case.id)
        .first()
    ):
        raise ApiError(404, "not_found", "Proceeding not found.")
    last_visit = since
    new_ids: set[int] = set()
    if last_visit is not None:
        new_ids = {
            row[0]
            for row in db.query(Document.id)
            .filter(
                Document.proceeding_id == proceeding, Document.ingest_date > last_visit
            )
            .all()
        }
    payload = CaseGraphService(db).build_payload(
        proceeding,
        filter,
        new_doc_ids=new_ids,
        reaction_map=reaction_map_for_proceeding(db, proceeding),
    )
    data = dataclasses.asdict(payload)
    return GraphView(
        proceeding_id=proceeding,
        filter=filter,
        lanes=data["lanes"],
        nodes=data["nodes"],
        bundles=data["bundles"],
        edges=data["edges"],
        proof_badges={str(k): v for k, v in data["proof_badges"].items()},
        svg_width=data["svg_width"],
        svg_height=data["svg_height"],
        node_counts=data["node_counts"],
        node_count=data["node_count"],
        edge_count=data["edge_count"],
    )


@router.get("/cases/{case_id}/timeline", response_model=TimelineView)
def case_timeline(
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access()),
):
    t = CaseTimelineService(db).build_payload(case.id)
    gaps = t.get("quiet_gaps") or {}
    return TimelineView(
        today=t["today"],
        total_count=t["total_count"],
        events=[
            TimelineEventView(**dataclasses.asdict(e), quiet_gap_days=gaps.get(e.id))
            for e in t["events"]
        ],
        month_buckets=t["month_buckets"],
    )


@router.get("/cases/{case_id}/financials", response_model=FinancialsView)
def case_financials(
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access()),
):
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
                invoices_own=[_cost_row(c) for c in row["invoices_own"]],
                invoices_court=[_cost_row(c) for c in row["invoices_court"]],
                invoices_opposing=[_cost_row(c) for c in row["invoices_opposing"]],
                invoices_other=[_cost_row(c) for c in row["invoices_other"]],
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
        summary=_summary(case, db),
        instances=instances,
        case_level_costs=[_cost_row(c) for c in build_case_level_costs(case.id, db)],
        signal_docs=signal_docs,
    )


# --- Proceedings -------------------------------------------------------------


@router.patch("/proceedings/{proceeding_id}", response_model=ProceedingView)
def update_proceeding(
    body: ProceedingUpdate,
    db: Session = Depends(get_db),
    proc: Proceeding = Depends(require_proceeding_access(edit=True)),
):
    data = body.model_dump(exclude_none=True)
    if data:
        ProceedingRepository(db).update(proc.id, **data)
        db.commit()
    return next(p for p in _proceedings(db, proc.case_id) if p.id == proc.id)


@router.delete("/proceedings/{proceeding_id}", status_code=204, response_class=Response)
def delete_proceeding(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    proc: Proceeding = Depends(require_proceeding_access(edit=True)),
):
    """Delete an empty proceeding; the last proceeding of a case is refused."""
    try:
        result = CaseService(db).delete_empty_proceeding(proc.id, user.id)
    except ValueError as e:
        msg = str(e)
        raise ApiError(
            404 if "not found" in msg.lower() else 409, "proceeding_not_empty", msg
        ) from e
    if result["was_active"]:
        user_settings_service.set_active_proceeding(
            result["case_id"], None, db, user.id
        )
        db.commit()


# --- Sharing -----------------------------------------------------------------


def _owned_case(
    case_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Case:
    """Only the owner or an admin manages shares; others get 404."""
    case = db.get(Case, case_id)
    if case is None or (not access_service.is_admin(user) and case.owner_id != user.id):
        raise ApiError(404, "not_found", "Case not found.")
    return case


def _sharing(db: Session, case: Case) -> SharingView:
    owner = db.get(User, case.owner_id) if case.owner_id else None
    rows = (
        db.query(CaseShare, User)
        .join(User, User.id == CaseShare.user_id)
        .filter(CaseShare.case_id == case.id)
        .order_by(User.email.asc())
        .all()
    )
    return SharingView(
        owner_email=owner.email if owner else None,
        shares=[
            ShareView(
                user_id=u.id,
                email=u.email,
                display_name=u.display_name,
                permission=s.permission,
            )
            for s, u in rows
        ],
    )


@router.get("/cases/{case_id}/shares", response_model=SharingView)
def list_shares(db: Session = Depends(get_db), case: Case = Depends(_owned_case)):
    return _sharing(db, case)


@router.post("/cases/{case_id}/shares", response_model=SharingView)
def add_share(
    body: ShareCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(_owned_case),
):
    target = auth_service.get_user_by_email(db, body.email.strip())
    if target is None:
        raise ApiError(404, "user_not_found", "No user with that email.")
    if target.id == case.owner_id:
        raise ApiError(409, "owner", "The owner already has full access.")
    existing = (
        db.query(CaseShare)
        .filter(CaseShare.case_id == case.id, CaseShare.user_id == target.id)
        .first()
    )
    if existing:
        existing.permission = body.permission
    else:
        db.add(
            CaseShare(
                case_id=case.id,
                user_id=target.id,
                permission=body.permission,
                granted_by=user.id,
            )
        )
    db.commit()
    return _sharing(db, case)


@router.delete("/cases/{case_id}/shares/{user_id}", response_model=SharingView)
def remove_share(
    user_id: int, db: Session = Depends(get_db), case: Case = Depends(_owned_case)
):
    share = (
        db.query(CaseShare)
        .filter(CaseShare.case_id == case.id, CaseShare.user_id == user_id)
        .first()
    )
    if share:
        db.delete(share)
        db.commit()
    return _sharing(db, case)
