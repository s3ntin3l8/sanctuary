import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db, get_triage_service
from app.helpers import render_page
from app.models.database import Case, Document
from app.models.enums import OriginatorType, UserReactionType
from app.services.document_service import DocumentService
from app.services.triage_service import TriageService

router = APIRouter(tags=["pages"])


# -----------------------------------------------------------------------------
# Triage page (GET)
# -----------------------------------------------------------------------------


@router.get("/triage")
async def triage_page(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    from app.models.database import Proceeding

    bundles = triage_service.get_triage_bundles(limit=limit, offset=offset)
    all_cases = db.query(Case).order_by(Case.title.asc()).all()
    total_docs = sum(b.doc_count for b in bundles)

    reactions_by_doc = {}
    for bundle in bundles:
        for doc in bundle.documents:
            reactions_by_doc[doc.id] = {
                r.reaction for r in triage_service.get_reactions(doc.id)
            }  # set for card-level membership check only

    proceedings = db.query(Proceeding).order_by(Proceeding.court_name.asc()).all()

    return render_page(
        request,
        "pages/triage.html",
        db=db,
        bundles=bundles,
        all_cases=all_cases,
        cases=all_cases,
        proceedings=proceedings,
        total_docs=total_docs,
        reactions_by_doc=reactions_by_doc,
        limit=limit,
        offset=offset,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        OriginatorType=OriginatorType,
        UserReactionType=UserReactionType,
    )


# -----------------------------------------------------------------------------
# Document confirm (metadata patch)
# -----------------------------------------------------------------------------


@router.post("/triage/document/{doc_id}/confirm")
async def confirm_document(
    request: Request,
    doc_id: int,
    title: str | None = Form(None),
    case_id: str | None = Form(None),
    originator_type: str | None = Form(None),
    sender: str | None = Form(None),
    received_date: str | None = Form(None),
    mark_resolved: str | None = Form(None),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    parsed_date = None
    if received_date:
        try:
            parsed_date = datetime.strptime(received_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid date: {received_date}"
            ) from exc

    parsed_originator = None
    if originator_type:
        try:
            parsed_originator = OriginatorType(originator_type)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Unknown originator: {originator_type}"
            ) from exc

    resolved_case_id = case_id if case_id else None

    doc = triage_service.confirm_document(
        doc_id,
        title=title,
        case_id=resolved_case_id,
        originator_type=parsed_originator,
        sender=sender,
        received_date=parsed_date,
        finalize=bool(mark_resolved),
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    # Return the updated HUD by re-rendering document_triage.html
    from app.helpers import build_document_extraction_context
    from app.models.database import Entity

    cases = db.query(Case).order_by(Case.title.asc()).all()
    entities = db.query(Entity).filter(Entity.source_document_id == doc.id).all()
    extraction_context = build_document_extraction_context(db, doc)
    reactions = list(triage_service.get_reactions(doc.id))
    action_items = triage_service.get_action_items(doc.id)

    response = templates.TemplateResponse(
        request,
        "partials/document_triage.html",
        {
            "doc": doc,
            "doc_id": doc.id,
            "cases": cases,
            "entities": entities,
            "context": extraction_context,
            "reactions": reactions,
            "action_items": action_items,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        },
    )

    # Confirm-and-next: if the doc is now out of triage, tell the client which
    # doc to advance to. Alpine listener picks this up from the HX-Trigger
    # header and shifts focus.
    if not doc.needs_review and doc.case_id and doc.case_id != "_TRIAGE":
        next_doc = triage_service.find_next_review_doc(doc.id)
        if next_doc:
            response.headers["HX-Trigger"] = json.dumps(
                {"triage:advance": {"next_doc_id": next_doc.id}}
            )
        else:
            response.headers["HX-Trigger"] = json.dumps({"triage:clear": {}})

    return response


# -----------------------------------------------------------------------------
# Bundle confirm (cascade assign case)
# -----------------------------------------------------------------------------


@router.post("/triage/bundle/{batch_id}/confirm")
async def confirm_bundle(
    request: Request,
    batch_id: int,
    case_id: str = Form(...),
    proceeding_id: str | None = Form(None),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    from app.models.database import Proceeding

    if not case_id:
        raise HTTPException(status_code=422, detail="case_id is required")

    parsed_proceeding_id = None
    if proceeding_id:
        try:
            parsed_proceeding_id = int(proceeding_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid proceeding_id: {proceeding_id}"
            ) from exc

    batch = triage_service.confirm_bundle(
        batch_id, case_id=case_id, proceeding_id=parsed_proceeding_id
    )
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

    # Re-render the whole feed
    bundles = triage_service.get_triage_bundles()
    reactions_by_doc = {}
    for bundle in bundles:
        for doc in bundle.documents:
            reactions_by_doc[doc.id] = {
                r.reaction for r in triage_service.get_reactions(doc.id)
            }  # set for card-level membership check only
    all_cases = db.query(Case).order_by(Case.title.asc()).all()
    proceedings = db.query(Proceeding).order_by(Proceeding.court_name.asc()).all()

    return templates.TemplateResponse(
        request,
        "partials/triage_feed.html",
        {
            "bundles": bundles,
            "all_cases": all_cases,
            "cases": all_cases,
            "proceedings": proceedings,
            "reactions_by_doc": reactions_by_doc,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
        },
    )


# -----------------------------------------------------------------------------
# Reaction Bar (POST/DELETE)
# -----------------------------------------------------------------------------


@router.post("/triage/document/{doc_id}/reaction")
async def set_reaction(
    request: Request,
    doc_id: int,
    reaction: str = Form(...),
    notes: str | None = Form(None),
    triage_service: TriageService = Depends(get_triage_service),
):
    try:
        reaction_enum = UserReactionType(reaction)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Unknown reaction: {reaction}"
        ) from exc

    triage_service.toggle_reaction(doc_id, reaction_enum, notes=notes)
    reactions = list(triage_service.get_reactions(doc_id))

    response = templates.TemplateResponse(
        request,
        "partials/triage_reaction_bar.html",
        {
            "doc": {"id": doc_id},
            "reactions": reactions,
            "UserReactionType": UserReactionType,
        },
    )
    if notes is not None and notes.strip():
        response.headers["HX-Trigger"] = json.dumps(
            {"triage:note-saved": {"message": "Note saved"}}
        )
    return response


@router.delete("/triage/document/{doc_id}/reaction/{reaction}")
async def clear_reaction(
    request: Request,
    doc_id: int,
    reaction: str,
    triage_service: TriageService = Depends(get_triage_service),
):
    try:
        reaction_enum = UserReactionType(reaction)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Unknown reaction: {reaction}"
        ) from exc

    triage_service.clear_reaction(doc_id, reaction_enum)
    reactions = list(triage_service.get_reactions(doc_id))

    return templates.TemplateResponse(
        request,
        "partials/triage_reaction_bar.html",
        {
            "doc": {"id": doc_id},
            "reactions": reactions,
            "UserReactionType": UserReactionType,
        },
    )


# -----------------------------------------------------------------------------
# Document actions (reingest, summarize, approve-summary)
# -----------------------------------------------------------------------------


@router.post("/document/{doc_id}/reingest")
async def reingest_document(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    from app.services.ingestion.service import process_uploaded_document

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    try:
        process_uploaded_document(doc, db)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Reingestion failed: {exc}"
        ) from exc

    # Re-render the HUD
    return await _render_document_hud(request, doc, db)


@router.post("/document/{doc_id}/summarize")
async def summarize_document(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    from app.services.ai_summary import _summarize_document_sync

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    try:
        _summarize_document_sync(doc_id, db)
    except Exception as exc:
        doc.ai_summary_status = "failed"
        doc.ai_summary = {"error": str(exc)}
        db.commit()

    db.refresh(doc)
    return await _render_document_hud(request, doc, db)


@router.post("/document/{doc_id}/approve-summary")
async def approve_summary(
    request: Request,
    doc_id: int,
    action: str,
    db: Session = Depends(get_db),
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    if action == "approve":
        doc.ai_summary_status = "approved"
        doc.ai_summary_approved_at = datetime.now()
    elif action == "reject":
        doc.ai_summary_status = "pending"
        doc.ai_summary = None
    else:
        raise HTTPException(status_code=422, detail=f"Unknown action: {action}")

    db.commit()
    db.refresh(doc)
    return await _render_document_hud(request, doc, db)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _render_document_hud(
    request: Request, doc: Document, db: Session
) -> HTMLResponse:
    """Render the HUD partial — reused by reingest/summarize/approve-summary/confirm."""
    from app.helpers import build_document_extraction_context
    from app.models.database import Entity

    triage_service = TriageService(db)
    cases = db.query(Case).order_by(Case.title.asc()).all()
    entities = db.query(Entity).filter(Entity.source_document_id == doc.id).all()
    extraction_context = build_document_extraction_context(db, doc)
    reactions = list(triage_service.get_reactions(doc.id))
    action_items = triage_service.get_action_items(doc.id)

    return templates.TemplateResponse(
        request,
        "partials/document_triage.html",
        {
            "doc": doc,
            "doc_id": doc.id,
            "cases": cases,
            "entities": entities,
            "context": extraction_context,
            "reactions": reactions,
            "action_items": action_items,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        },
    )


# -----------------------------------------------------------------------------
# Activity log (unchanged — carried over from the old triage.py to keep routes intact)
# -----------------------------------------------------------------------------


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

    docs = (
        db.query(Document)
        .order_by(Document.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    return templates.TemplateResponse(
        request,
        "partials/activity_feed_items.html",
        {
            "documents": docs,
            "case_titles": case_titles,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        },
    )
