from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Case
from app.services.document_service import DocumentService

router = APIRouter(tags=["pages"])


@router.get("/contacts/{sender_name}")
async def sender_detail(
    request: Request, sender_name: str, db: Session = Depends(get_db)
):
    from urllib.parse import unquote

    sender = unquote(sender_name)
    doc_service = DocumentService(db)
    docs = doc_service.get_documents_by_sender(sender)

    case_ids = {d.case_id for d in docs if d.case_id}
    cases = {}
    if case_ids:
        cases = {
            c.id: c.title for c in db.query(Case).filter(Case.id.in_(case_ids)).all()
        }

    context = {
        "sender": sender,
        "documents": docs,
        "cases": cases,
        "originator_colors": ORIGINATOR_COLORS,
        "originator_icons": ORIGINATOR_ICONS,
    }

    if request.headers.get("hx-request"):
        return templates.TemplateResponse(
            request, "partials/sender_detail_content.html", context
        )

    return render_page(request, "pages/sender_detail.html", db=db, **context)
