from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import (
    CASE_STATUS_META,
    COST_CATEGORY_META,
    ORIGINATOR_COLORS,
    ORIGINATOR_ICONS,
)
from app.dependencies import get_db
from app.helpers import build_cost_summary, render_page
from app.models.database import (
    CostStatus,
    EntityType,
)
from app.services.case_service import CaseService

router = APIRouter(prefix="/cases", tags=["pages"])

DEFAULT_PAGE_SIZE = 20


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


@router.get("/{case_id:path}")
async def case_detail(request: Request, case_id: str, db: Session = Depends(get_db)):
    case_service = CaseService(db)
    data = case_service.get_case_with_summary(case_id)

    if not data:
        return templates.TemplateResponse(
            "errors/404.html",
            {
                "request": request,
                "message": f"Case {case_id} not found",
                "sidebar_counts": {
                    "triage_count": 0,
                    "cases_active": 0,
                    "cases_total": 0,
                    "docs_pending": 0,
                    "costs_pending": 0,
                },
            },
            status_code=404,
        )

    cost_summary = build_cost_summary(data["costs"], CostStatus)

    from collections import defaultdict

    resolved_docs = [d for d in data["documents"] if not d.needs_review]
    resolved_by_month = defaultdict(list)
    for d in sorted(resolved_docs, key=lambda x: x.created_at, reverse=True):
        month_key = d.created_at.strftime("%B %Y")
        resolved_by_month[month_key].append(d)

    entities_dict = {
        "persons": [e for e in data["entities"] if e.type == EntityType.PERSON],
        "organizations": [
            e for e in data["entities"] if e.type == EntityType.ORGANIZATION
        ],
        "dates": [e for e in data["entities"] if e.type == EntityType.DATE],
        "financial": [e for e in data["entities"] if e.type == EntityType.FINANCIAL],
        "legal_categories": [
            e for e in data["entities"] if e.type == EntityType.LEGAL_CATEGORY
        ],
    }

    return render_page(
        request,
        "pages/case_stream.html",
        db=db,
        case=data["case"],
        case_title=data["case"].title,
        case_status=data["case"].status,
        documents=data["documents"],
        review_docs=[d for d in data["documents"] if d.needs_review],
        resolved_by_month=dict(resolved_by_month),
        entities=entities_dict,
        cost_summary=cost_summary,
        deadlines=data["deadlines"],
        hearings=data["hearings"],
        all_entities=data["entities"],
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        status_meta=CASE_STATUS_META,
        cost_category_meta=COST_CATEGORY_META,
    )
