from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Case, Document
from app.services.document_service import DocumentService

router = APIRouter(tags=["pages"])


@router.get("/triage")
async def triage_page(request: Request, db: Session = Depends(get_db)):
    doc_service = DocumentService(db)
    docs = doc_service.get_triage_documents()

    all_cases = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    return render_page(
        request,
        "pages/triage.html",
        db=db,
        documents=docs,
        all_cases=all_cases,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
    )


@router.get("/activity")
async def activity_log(request: Request, db: Session = Depends(get_db)):
    doc_service = DocumentService(db)
    data = doc_service.get_activity_feed(limit=20)

    total_docs = db.query(Document).count()
    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}
    all_cases = db.query(Case).order_by(Case.created_at.desc()).all()

    return render_page(
        request,
        "pages/activity_log.html",
        db=db,
        documents=data["recent_documents"],
        total_docs=total_docs,
        pending_docs=data["pending_documents"],
        case_titles=case_titles,
        all_cases=all_cases,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
    )


@router.get("/api/activity-feed")
async def activity_feed_hx(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """HTMX endpoint for infinite scroll activity feed."""
    from app.models.database import Document

    docs = (
        db.query(Document)
        .order_by(Document.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    return templates.TemplateResponse(
        "partials/activity_feed_items.html",
        {
            "request": request,
            "documents": docs,
            "case_titles": case_titles,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        },
    )
