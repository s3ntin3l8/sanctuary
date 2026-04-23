import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db
from app.helpers import format_relative_time, render_page
from app.models.database import Case, Document

router = APIRouter(prefix="/timeline", tags=["api"])


@router.get("")
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
        format_relative_time=format_relative_time,
    )


@router.get("/data")
async def timeline_api(
    request: Request,
    cursor: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """HTMX endpoint for timeline pagination — returns HTML partial."""
    from app.services.document_service import DocumentService

    doc_service = DocumentService(db)
    docs, has_more = doc_service.get_documents_paginated(cursor=cursor, limit=limit + 1)

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}
    next_cursor = docs[-1].id if docs and has_more else None

    html = templates.get_template("partials/timeline_items.html").render(
        {
            "docs": docs,
            "case_titles": case_titles,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        }
    )

    trigger_data = json.dumps(
        {"timeline-paginated": {"has_more": has_more, "next_cursor": next_cursor}}
    )
    return HTMLResponse(content=html, headers={"HX-Trigger": trigger_data})
