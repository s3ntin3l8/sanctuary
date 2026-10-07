"""Correspondents: the documents a sender has sent across the caller's cases."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db
from app.models.database import Case, Document, User
from app.schemas.contacts import ContactCase, ContactDocument, ContactView
from app.services import access_service
from app.services.document_service import DocumentService

router = APIRouter(tags=["contacts"])

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _when(d: Document) -> datetime:
    return d.issued_date or d.ingest_date or _EPOCH


@router.get("/contacts", response_model=ContactView)
def contact(
    name: str = Query(min_length=1, max_length=300),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Documents whose sender contains ``name``, limited to the caller's cases."""
    docs = list(DocumentService(db).get_documents_by_sender(name))
    visible = access_service.visible_case_ids(db, user)
    if visible is not None:
        docs = [d for d in docs if d.case_id in visible]
    docs.sort(key=_when, reverse=True)
    case_ids = {d.case_id for d in docs if d.case_id and d.case_id != "_TRIAGE"}
    cases = (
        db.query(Case).filter(Case.id.in_(case_ids)).order_by(Case.title.asc()).all()
        if case_ids
        else []
    )
    titles = {c.id: c.title for c in cases}
    dated = [_when(d) for d in docs if d.issued_date or d.ingest_date]
    return ContactView(
        name=name,
        document_count=len(docs),
        case_count=len(cases),
        last_contact=max(dated) if dated else None,
        cases=[ContactCase(id=c.id, title=c.title, status=c.status) for c in cases],
        documents=[
            ContactDocument(
                id=d.id,
                title=d.title,
                case_id=d.case_id,
                case_title=titles.get(d.case_id or ""),
                originator_type=d.originator_type,
                issued_date=d.issued_date,
                ingest_date=d.ingest_date,
                legal_significance=(
                    d.ai_summary.get("legal_significance")
                    if isinstance(d.ai_summary, dict)
                    else None
                ),
            )
            for d in docs
        ],
    )
