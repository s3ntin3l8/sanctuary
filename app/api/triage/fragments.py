"""HTMX fragment endpoints: relationship confirm/reject, doc HUD/body partials."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.dependencies import get_current_user, get_db
from app.models.database import Document, DocumentRelationship, User
from app.repositories.case import CaseRepository
from app.services.hud_context import build_hud_context

router = APIRouter()


def _require_owned_rel(db: Session, rel_id: int, user: User) -> DocumentRelationship:
    """Fetch a relationship, 404ing unless both its documents belong to the user."""
    rel = (
        db.query(DocumentRelationship).filter(DocumentRelationship.id == rel_id).first()
    )
    if not rel:
        raise HTTPException(status_code=404, detail=f"Relationship {rel_id} not found")
    doc_ids = [rel.from_document_id, rel.to_document_id]
    owners = {
        row[0]
        for row in db.query(Document.owner_id).filter(Document.id.in_(doc_ids)).all()
    }
    if owners - {user.id}:
        raise HTTPException(status_code=404, detail=f"Relationship {rel_id} not found")
    return rel


@router.post("/triage/relationship/{rel_id}/confirm")
async def confirm_relationship(
    request: Request,
    rel_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Promote an AI-detected relationship to user-confirmed, closing the target's thread."""
    from app.models.enums import RelationshipConfidence
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    rel = _require_owned_rel(db, rel_id, user)

    source_id = rel.from_document_id
    rel.confidence = RelationshipConfidence.USER_CONFIRMED
    db.commit()
    recompute_thread_open(rel.to_document_id, db)
    source_doc = db.query(Document).filter(Document.id == source_id).first()
    if source_doc:
        refresh_review_reasons(source_doc, db)
    return HTMLResponse("")


@router.delete("/triage/relationship/{rel_id}")
async def reject_relationship(
    request: Request,
    rel_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Remove a relationship suggestion, reopening the target's thread if no confirmed edges remain."""
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    rel = _require_owned_rel(db, rel_id, user)

    target_id = rel.to_document_id
    source_id = rel.from_document_id
    db.delete(rel)
    db.commit()
    recompute_thread_open(target_id, db)
    source_doc = db.query(Document).filter(Document.id == source_id).first()
    if source_doc:
        refresh_review_reasons(source_doc, db)
    return HTMLResponse("")


@router.get("/triage/doc/{doc_id}/hud")
def triage_doc_hud(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    from sqlalchemy.orm import joinedload as _joinedload

    doc = (
        db.query(Document)
        .options(_joinedload(Document.proceeding))
        .filter(Document.id == doc_id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    cases = CaseRepository(db).list_for_picker()
    ctx = build_hud_context(db, doc, mode="review", context="embedded", cases=cases)
    return templates.TemplateResponse(request, "partials/triage/_doc_hud.html", ctx)


@router.get("/triage/doc/{doc_id}/body")
def triage_doc_body(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """Return just the doc body (Docling markdown + highlighted passages).

    Used by the triage drawer's middle column. Same context as /hud — the
    body partial only reads doc + key_passages + passage_claim_map + pins.
    """
    from sqlalchemy.orm import joinedload as _joinedload

    doc = (
        db.query(Document)
        .options(_joinedload(Document.proceeding))
        .filter(Document.id == doc_id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    ctx = build_hud_context(db, doc, mode="read", context="embedded")
    return templates.TemplateResponse(request, "partials/hud/_body.html", ctx)
