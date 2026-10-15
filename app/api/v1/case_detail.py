"""The case dashboard: detail, brief, graph, timeline, financials, sharing,
and the case/proceeding mutations the dashboard offers."""

from __future__ import annotations

import dataclasses
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.access_guards import (
    require_action_item_access,
    require_case_access,
    require_proceeding_access,
)
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.core.validators import normalize_case_id
from app.dependencies import get_current_user, get_db
from app.models.database import (
    ActionItem,
    Case,
    CaseShare,
    Document,
    Proceeding,
    User,
)
from app.models.enums import (
    BriefState,
    CaseStatus,
    ProceedingStatus,
)
from app.repositories.proceeding import ProceedingRepository
from app.schemas.case_detail import (
    BriefView,
    CaseActionItem,
    CaseDetail,
    CaseMerge,
    CaseMergeResult,
    CasePurge,
    CaseUpdate,
    FinancialsView,
    GraphView,
    OpposingPartiesUpdate,
    PartyView,
    ProceedingMerge,
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
from app.services import (
    access_service,
    action_item_service,
    api_token_service,
    auth_service,
    user_settings_service,
)
from app.services.case_dashboard_service import (
    reaction_map_for_proceeding,
)
from app.services.case_graph_service import CaseGraphService
from app.services.case_overview_service import (
    action_item_view,
    build_case_detail,
    build_financials,
    proceeding_views,
    serialize_brief,
)
from app.services.case_service import (
    CaseService,
    set_case_opposing_parties,
)
from app.services.case_timeline_service import CaseTimelineService
from app.services.ingestion.extractors import canonical_az_court
from app.services.merge_service import (
    MergeError,
    merge_cases,
    merge_proceedings,
    rename_case,
)

router = APIRouter(tags=["cases"])


# --- Helpers -----------------------------------------------------------------


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


# --- Detail ------------------------------------------------------------------


@router.get("/cases/{case_id}", response_model=CaseDetail)
def case_detail(
    proceeding: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access()),
):
    """Everything the dashboard shell needs; marks the case as viewed."""
    procs = proceeding_views(db, case.id)
    active = _resolve_active(db, user, case.id, procs, proceeding)
    last_visit = user_settings_service.get_last_viewed(case.id, db, user.id)
    return build_case_detail(
        db,
        user,
        case,
        proceedings=procs,
        active_proceeding_id=active,
        last_visit=last_visit,
    )


@router.post("/cases/{case_id}/viewed", status_code=204, response_class=Response)
def mark_case_viewed(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access()),
):
    """Record the visit once the dashboard has rendered its "new since" markers."""
    user_settings_service.mark_viewed(case.id, db, user.id)
    db.commit()


def _rename(db: Session, user: User, case: Case, new_id: str) -> Case:
    existing = db.get(Case, normalize_case_id(new_id) or "")
    if existing is not None and not access_service.can_edit_case(db, user, existing):
        # Same answer as for a missing case: never confirm a case the caller can't edit.
        raise ApiError(404, "not_found", "Case not found.")
    try:
        return rename_case(db, case.id, new_id, user.id)
    except MergeError as exc:
        status = {"not_found": 404, "invalid_case_id": 422}.get(exc.code, 409)
        raise ApiError(status, exc.code, str(exc)) from exc


@router.patch("/cases/{case_id}", response_model=CaseDetail)
def update_case(
    body: CaseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access(edit=True)),
):
    if body.case_id is not None and body.case_id != case.id:
        # Renamed first and committed on its own: a refused ID leaves every other
        # field of this request unsaved too.
        case = _rename(db, user, case, body.case_id)
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
    item = action_item_service.set_status(db, item, body.status)
    names = api_token_service.token_names(db, [item.agent_token_id])
    return action_item_view(item, now_utc(), names)


# --- Brief -------------------------------------------------------------------


@router.get("/cases/{case_id}/brief", response_model=BriefView)
def get_brief(case: Case = Depends(require_case_access())):
    return serialize_brief(case)


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
    return serialize_brief(case)


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
    return build_financials(db, case)


# --- Proceedings -------------------------------------------------------------


@router.patch("/proceedings/{proceeding_id}", response_model=ProceedingView)
def update_proceeding(
    body: ProceedingUpdate,
    db: Session = Depends(get_db),
    proc: Proceeding = Depends(require_proceeding_access(edit=True)),
):
    data = body.model_dump(exclude_none=True)
    if "az_court" in data:
        data["az_court"] = canonical_az_court(data["az_court"])
    if data:
        ProceedingRepository(db).update(proc.id, **data)
        db.commit()
    return next(p for p in proceeding_views(db, proc.case_id) if p.id == proc.id)


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


def _merge_error(exc: MergeError) -> ApiError:
    return ApiError(404 if exc.code == "not_found" else 409, exc.code, str(exc))


@router.post("/proceedings/{proceeding_id}/merge", response_model=ProceedingView)
def merge_proceeding(
    body: ProceedingMerge,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    proc: Proceeding = Depends(require_proceeding_access(edit=True)),
):
    """Fold this proceeding into another of the same case, then delete it."""
    case_id = proc.case_id
    try:
        target = merge_proceedings(db, proc.id, body.target_id, user.id)
    except MergeError as exc:
        raise _merge_error(exc) from exc
    active = user_settings_service.get_active_proceeding(case_id, db, user.id)
    if active == proc.id:
        # merge_proceedings has already committed; this is a separate write.
        user_settings_service.set_active_proceeding(case_id, target.id, db, user.id)
        db.commit()
    return next(p for p in proceeding_views(db, case_id) if p.id == target.id)


@router.post("/cases/{case_id}/merge", response_model=CaseMergeResult)
def merge_case(
    body: CaseMerge,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Fold this case into another the user can edit, then delete it."""
    target = db.get(Case, body.target_id)
    if target is None or not access_service.can_edit_case(db, user, target):
        raise ApiError(404, "not_found", "Target case not found.")
    try:
        merged = merge_cases(db, case.id, target.id, user.id)
    except MergeError as exc:
        raise _merge_error(exc) from exc
    return CaseMergeResult(case_id=merged.id)


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
