from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session, joinedload

from app.config import AI_BASE_URL
from app.constants import CASE_STATUS_META, ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db
from app.helpers import (
    format_deadline_badge,
    format_relative_time,
    format_upcoming_datetime,
    render_page,
)
from app.models.database import (
    Case,
    CaseStatus,
    CostStatus,
    Document,
    LegalCost,
    OriginatorType,
)
from app.services.ai_summary import check_ollama_status
from app.services.embeddings import check_embedding_status
from app.services.search_service import SearchService

router = APIRouter(prefix="", tags=["pages"])


@router.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    now = datetime.now()
    week_ago = now - timedelta(days=7)

    search_service = SearchService(db)
    data = search_service.get_dashboard_data()

    all_cases = db.query(Case).order_by(Case.created_at.desc()).all()
    case_titles = {c.id: c.title for c in all_cases}

    active_cases = [case for case in all_cases if case.status != CaseStatus.CLOSED]
    active_case_count = len(active_cases)
    new_active_cases_this_week = sum(
        1
        for case in active_cases
        if case.created_at
        and (
            case.created_at.replace(tzinfo=UTC)
            if case.created_at.tzinfo is None
            else case.created_at
        )
        >= week_ago.replace(tzinfo=UTC)
    )

    pending_docs = data["pending_documents"]
    pending_review_count = len(pending_docs)
    pending_added_this_week = sum(
        1
        for doc in pending_docs
        if doc.created_at
        and (
            doc.created_at.replace(tzinfo=UTC)
            if doc.created_at.tzinfo is None
            else doc.created_at
        )
        >= week_ago.replace(tzinfo=UTC)
    )

    court_doc_count = (
        db.query(Document)
        .filter(Document.originator_type == OriginatorType.COURT)
        .count()
    )
    new_documents_this_week = (
        db.query(Document).filter(Document.created_at >= week_ago).count()
    )

    priority_docs = pending_docs[:4]
    recent_documents = data["recent_documents"]
    active_case_snapshot = active_cases[:4]
    upcoming_deadlines = data["upcoming_deadlines"]
    upcoming_hearings = data["upcoming_hearings"]

    overdue_costs = (
        db.query(LegalCost)
        .filter(
            LegalCost.due_at < now,
            LegalCost.status.notin_([CostStatus.BEZAHLT, CostStatus.ERSTATTET]),
        )
        .order_by(LegalCost.due_at.asc())
        .limit(4)
        .all()
    )

    status_summary = await check_ollama_status()
    embed_status = await check_embedding_status()
    ai_status = {
        "reachable": status_summary["reachable"],
        "summary_model": status_summary["summary_model"],
        "embedding_model": embed_status["embedding_model"],
        "error": status_summary["error"] or embed_status["error"],
    }

    return render_page(
        request,
        "pages/dashboard.html",
        db=db,
        ai_status=ai_status,
        ai_base_url=AI_BASE_URL,
        active_case_count=active_case_count,
        new_active_cases_this_week=new_active_cases_this_week,
        pending_review_count=pending_review_count,
        pending_added_this_week=pending_added_this_week,
        court_doc_count=court_doc_count,
        new_documents_this_week=new_documents_this_week,
        priority_docs=priority_docs,
        recent_documents=recent_documents,
        active_case_snapshot=active_case_snapshot,
        upcoming_deadlines=upcoming_deadlines,
        upcoming_hearings=upcoming_hearings,
        overdue_costs=overdue_costs,
        case_titles=case_titles,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        status_meta=CASE_STATUS_META,
        format_relative_time=format_relative_time,
        format_upcoming_datetime=format_upcoming_datetime,
        format_deadline_badge=format_deadline_badge,
    )


@router.get("/timeline")
async def timeline_page(
    request: Request,
    page: int = 1,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """Master Timeline with pagination support."""
    offset = (page - 1) * limit

    all_cases = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    total_docs = db.query(Document).count()

    query = (
        db.query(Document)
        .options(joinedload(Document.children))
        .order_by(Document.created_at.desc())
    )
    documents = query.offset(offset).limit(limit + 1).all()
    has_more = len(documents) > limit
    if has_more:
        documents = documents[:limit]

    pending_count = db.query(Document).filter(Document.needs_review).count()

    grouped_docs = {}
    for doc in documents:
        period = doc.created_at.strftime("%B %Y")
        if period not in grouped_docs:
            grouped_docs[period] = []
        grouped_docs[period].append(doc)

    return render_page(
        request,
        "pages/timeline.html",
        db=db,
        grouped_docs=grouped_docs,
        case_titles=all_cases,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        total_docs=total_docs,
        pending_count=pending_count,
        current_page=page,
        has_more=has_more,
        per_page=limit,
    )
