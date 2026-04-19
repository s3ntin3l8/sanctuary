from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import (
    CASE_STATUS_META,
    ORIGINATOR_COLORS,
)
from app.dependencies import get_db
from app.helpers import build_cost_summary, render_page
from app.models.database import (
    Case,
    CostStatus,
    Document,
)
from app.models.enums import ProceedingStatus
from app.services.case_service import CaseService
from app.services.user_settings_service import mark_viewed

router = APIRouter(prefix="/cases", tags=["pages"])

DEFAULT_PAGE_SIZE = 20
DORMANCY_DAYS = 90


def _compute_dormancy_alert(case, db) -> str | None:
    now = datetime.now()
    active_procs = [
        p for p in (case.proceedings or []) if p.status == ProceedingStatus.ACTIVE
    ]
    if not active_procs:
        return None

    oldest_silent_proc = None
    oldest_days = 0

    for proc in active_procs:
        last_activity = (
            db.query(func.max(Document.created_at))
            .filter(Document.proceeding_id == proc.id)
            .scalar()
        )
        if last_activity is None:
            last_activity = proc.started_at or proc.created_at
        if last_activity is None:
            continue
        days = (now - last_activity).days
        if days > DORMANCY_DAYS and days > oldest_days:
            oldest_silent_proc = proc
            oldest_days = days

    if oldest_silent_proc is None:
        return None

    court = oldest_silent_proc.court_name or "Unknown court"
    az = oldest_silent_proc.az_court or "no docket"
    return f"{court} ({az}) has had no activity for {oldest_days} days."


@router.get("")
async def case_directory(
    request: Request, page: int = 1, db: Session = Depends(get_db)
):
    case_service = CaseService(db)

    if page > 1:
        data = case_service.get_all_cases_directory_paginated(
            page=page, per_page=DEFAULT_PAGE_SIZE
        )
    else:
        data = case_service.get_all_cases_directory()

    case_titles = {c.id: c.title for c in data["cases"]}

    return render_page(
        request,
        "pages/case_directory.html",
        db=db,
        all_cases=data["cases"],
        active_cases=data["active_cases"],
        closed_cases=data["closed_cases"],
        case_titles=case_titles,
        stats_by_status=data["stats_by_status"],
        doc_counts=data["doc_counts"],
        deadline_counts=data["deadline_counts"],
        status_meta=CASE_STATUS_META,
        current_page=data.get("page", 1),
        total_pages=data.get("total_pages", 1),
        total=data["total"],
    )


@router.get("/{case_id}/brief")
async def case_brief_partial(
    request: Request, case_id: str, db: Session = Depends(get_db)
):
    """HTMX polling endpoint — returns the brief panel partial."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return HTMLResponse(content="<p>Case not found</p>", status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/case_brief_panel.html",
        {
            "request": request,
            "case": case,
            "brief": case.ai_brief,
            "ai_brief_updated_at": case.ai_brief_updated_at,
        },
    )


@router.post("/{case_id}/brief/refresh")
async def case_brief_refresh(
    request: Request, case_id: str, db: Session = Depends(get_db)
):
    """Set brief to processing, enqueue refresh task, return spinner partial."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return HTMLResponse(content="<p>Case not found</p>", status_code=404)

    case.ai_brief = {"status": "processing"}
    db.commit()

    from app.tasks.generate_case_brief import refresh_case_brief_task

    refresh_case_brief_task.delay(case_id)

    return templates.TemplateResponse(
        request,
        "partials/case_brief_panel.html",
        {
            "request": request,
            "case": case,
            "brief": {"status": "processing"},
            "ai_brief_updated_at": None,
        },
    )


@router.get("/{case_id}")
async def case_detail(request: Request, case_id: str, db: Session = Depends(get_db)):
    case_service = CaseService(db)
    data = case_service.get_case_with_summary(case_id)

    if not data:
        response = render_page(
            request,
            "errors/404.html",
            db=db,
            message=f"Case {case_id} not found",
        )
        response.status_code = 404
        return response

    cost_summary = build_cost_summary(data["costs"], CostStatus)
    dormancy_alert = _compute_dormancy_alert(data["case"], db)

    response = render_page(
        request,
        "pages/case_dashboard.html",
        db=db,
        case=data["case"],
        documents=data["documents"],
        deadlines=data["deadlines"],
        hearings=data["hearings"],
        brief=data["case"].ai_brief,
        ai_brief_updated_at=data["case"].ai_brief_updated_at,
        parties=data["case"].parties or [],
        total_cost_exposure=data["case"].total_cost_exposure or 0,
        cost_summary=cost_summary,
        count=data["new_docs_since_last_visit"],
        since=data["last_visit"],
        dormancy_alert=dormancy_alert,
        originator_colors=ORIGINATOR_COLORS,
        status_meta=CASE_STATUS_META,
    )

    mark_viewed(case_id, db)
    db.commit()

    return response
