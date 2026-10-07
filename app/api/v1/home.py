"""The Home dashboard."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.v1.cases import case_cards
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import HomeBriefing, IngestBatch, User
from app.schemas.home import (
    BriefingView,
    HomeActionItem,
    HomeActivityEvent,
    HomeDeltaCase,
    HomeSignal,
    HomeTriageBundle,
    HomeView,
    PipelineCounts,
)
from app.services import access_service
from app.services.case_service import CaseService
from app.services.home_service import HomeService
from app.services.intelligence.home_briefing_generator import (
    briefing_row,
    claim_for_dispatch,
    today_for_user,
)
from app.services.user_settings_service import mark_home_visit

router = APIRouter(prefix="/home", tags=["home"])


def _triage_bundle(batch: IngestBatch) -> HomeTriageBundle:
    docs = batch.documents
    confirmed = batch.case_id if batch.case_id and batch.case_id != "_TRIAGE" else None
    suggested = None
    if confirmed is None:
        suggested = next(
            (d.case_id for d in docs if d.case_id and d.case_id != "_TRIAGE"), None
        )
    summary = batch.pipeline_summary
    return HomeTriageBundle(
        id=batch.id,
        status=batch.status,
        received_at=batch.received_at,
        sender_email=batch.sender_email,
        title=batch.subject or (docs[0].title if docs else "Untitled bundle"),
        doc_count=len(docs),
        case_id=confirmed,
        suggested_case_id=suggested,
        pipeline=PipelineCounts(
            total=summary["total"],
            running=summary.get("running", 0) + summary.get("partial", 0),
            pending=summary.get("pending", 0),
            failed=summary.get("failed", 0),
            completed=summary.get("completed", 0),
        ),
    )


@router.get("", response_model=HomeView)
def home(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = HomeService(db).get_home_data(user.id)
    service = CaseService(db)
    editable = access_service.editable_case_ids(db, user)
    return HomeView(
        greeting=data["greeting"],
        user_name=data["user_name"],
        now=data["now"],
        today_items=[
            HomeActionItem(
                id=item.id,
                case_id=item.case_id,
                case_title=item.case.title if item.case else item.case_id,
                title=item.title,
                description=item.description,
                due_date=item.due_date,
                action_type=item.action_type,
            )
            for item in data["today_items"]
        ],
        triage_bundles=[_triage_bundle(b) for b in data["triage_bundles"]],
        last_home_visit=data["last_home_visit"],
        delta_cases=[HomeDeltaCase(**d) for d in data["delta_cases"]],
        signals=[HomeSignal(**s) for s in data["signals"]],
        activity=[HomeActivityEvent(**e) for e in data["activity"]],
        draft_cases=case_cards(service, data["draft_cases"], editable=editable),
        active_cases=case_cards(service, data["active_cases"], editable=editable),
        caught_up=data["caught_up"],
    )


@router.post("/review-all", status_code=204, response_class=Response)
def review_all(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Mark everything new as reviewed: advances last_home_visit to now."""
    mark_home_visit(db, user.id)
    db.commit()


# --- Morning briefing --------------------------------------------------------


def _briefing_view(row: HomeBriefing) -> BriefingView:
    return BriefingView(
        status=row.status,  # type: ignore[arg-type]
        day=row.day,
        generated_at=row.generated_at,
        model_label=row.model_label,
        external=bool(row.external),
        summary=row.summary,
        priorities=list(row.priorities or []),
        error=row.error,
    )


def _dispatch_briefing(db: Session, user_id: int, day: date) -> None:
    from app.tasks.dispatch import dispatch_task
    from app.tasks.generate_home_briefing import generate_home_briefing_task

    if claim_for_dispatch(db, user_id, day):
        dispatch_task(generate_home_briefing_task, user_id, day.isoformat())


@router.get("/briefing", response_model=BriefingView)
def get_briefing(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Today's briefing. The first request of the day starts generation and
    returns ``processing``; the client polls until ``ready`` or ``failed``."""
    day = today_for_user()
    row = briefing_row(db, user.id, day)
    # No row yet, or a run whose claim went stale (worker died): the claim
    # decides whether anything is dispatched, so a live run is never doubled.
    if row is None or row.status == "processing":
        _dispatch_briefing(db, user.id, day)
        row = briefing_row(db, user.id, day)
        if row is None:  # pragma: no cover — the claim just inserted it
            raise ApiError(500, "briefing_unavailable", "Could not start the briefing.")
    return _briefing_view(row)


@router.post("/briefing/refresh", response_model=BriefingView, status_code=202)
@limiter.limit("6/hour")
def refresh_briefing(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Regenerate today's briefing. A run already in flight is left alone."""
    day = today_for_user()
    _dispatch_briefing(db, user.id, day)
    row = briefing_row(db, user.id, day)
    if row is None:  # pragma: no cover
        raise ApiError(500, "briefing_unavailable", "Could not start the briefing.")
    return _briefing_view(row)
