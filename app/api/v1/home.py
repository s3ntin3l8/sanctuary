"""The Home dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.v1.cases import case_cards
from app.dependencies import get_current_user, get_db
from app.models.database import IngestBatch, User
from app.schemas.home import (
    HomeActionItem,
    HomeDeltaCase,
    HomeSignal,
    HomeTriageBundle,
    HomeView,
    PipelineCounts,
)
from app.services import access_service
from app.services.case_service import CaseService
from app.services.home_service import HomeService
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
        draft_cases=case_cards(service, data["draft_cases"], editable=editable),
        active_cases=case_cards(service, data["active_cases"], editable=editable),
        caught_up=data["caught_up"],
    )


@router.post("/review-all", status_code=204, response_class=Response)
def review_all(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Mark everything new as reviewed: advances last_home_visit to now."""
    mark_home_visit(db, user.id)
    db.commit()
