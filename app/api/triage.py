import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db, get_triage_service
from app.helpers import render_page
from app.models.database import Case, Document, DocumentRelationship
from app.models.enums import OriginatorType, UserReactionType
from app.services.hud_context import build_triage_hud_context
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
    slicing_queue = triage_service.get_slicing_queue()
    all_cases = (
        db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    )
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
        slicing_queue=slicing_queue,
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
        finalize=True,
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    cases = db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    ctx = build_triage_hud_context(db, doc, cases=cases, OriginatorType=OriginatorType)
    response = templates.TemplateResponse(request, "partials/hud/_container.html", ctx)
    # Targeted OOB: update only the affected card + bundle footer + badge.
    targeted_oob = _render_doc_targeted_oob(request, doc, triage_service, db)
    response.body += targeted_oob.encode("utf-8")

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
# Unified confirm — static endpoint so the modal form never needs a dynamic URL
# -----------------------------------------------------------------------------


@router.post("/triage/confirm")
async def confirm(
    request: Request,
    batch_id: str | None = Form(None),
    doc_id: str | None = Form(None),
    is_synthetic: str = Form("false"),
    action: str = Form("confirm_bundle"),
    active_doc_id: str | None = Form(None),
    case_id: str = Form(...),
    proceeding_id: str | None = Form(None),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Single POST target for the bundle-confirm modal.

    Uses targeted OOB swaps instead of full feed replacement so:
    - scroll position is preserved
    - Alpine activeDoc highlight persists
    - HUD pane is refreshed with the updated doc data (avoids stale form)

    action=assign_case   → cascade case_id, batch stays in triage
    action=confirm_bundle → cascade case_id + mark batch COMPLETED
    """
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

    finalize = action == "confirm_bundle"

    # ---- perform the DB update ----
    if is_synthetic == "true" and doc_id:
        _doc_id = int(doc_id)
        bundle_key = f"loose-{_doc_id}"
        updated_doc = triage_service.confirm_document(_doc_id, case_id=case_id)
        if not updated_doc:
            raise HTTPException(status_code=404, detail=f"Document {_doc_id} not found")
        if parsed_proceeding_id is not None:
            updated_doc.proceeding_id = parsed_proceeding_id
            db.commit()
            db.refresh(updated_doc)
    else:
        if not batch_id:
            raise HTTPException(
                status_code=422, detail="batch_id is required for bundle confirm"
            )
        _batch_id = int(batch_id)
        bundle_key = f"batch-{_batch_id}"
        batch = triage_service.confirm_bundle(
            _batch_id,
            case_id=case_id,
            proceeding_id=parsed_proceeding_id,
            finalize=finalize,
        )
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {_batch_id} not found")

    # ---- build targeted OOB response (no full feed replacement) ----
    bundles = triage_service.get_triage_bundles()
    updated_bundle = next((b for b in bundles if b.key == bundle_key), None)

    oob_parts: list[str] = []
    trigger: dict = {"triage:bundle-confirmed": {}}

    if updated_bundle:
        # Bundle still in triage — OOB-swap the whole bundle group (updates
        # case chip in header, all cards, footer, badge in one shot).
        oob_parts.append(
            _render_bundle_group_oob(request, updated_bundle, triage_service)
        )
        # Advance to the first doc in the bundle. triage:advance calls card.click()
        # which sets activeDoc (ring) and fires hx-get to reload the HUD — that
        # GET sees the committed case_id, so the metadata form is up-to-date.
        # Doing this instead of an OOB HUD swap avoids the HTMX race condition
        # where the GET response could arrive and overwrite the OOB swap.
        first_bundle_doc_id = (
            updated_bundle.documents[0].id if updated_bundle.documents else None
        )
        if first_bundle_doc_id:
            trigger["triage:advance"] = {
                "next_doc_id": first_bundle_doc_id,
                "scroll": False,
            }
    else:
        # Bundle left triage (finalized or was last-item synthetic) → delete from DOM.
        oob_parts.append(
            f'<div id="triage-bundle-group-{bundle_key}" hx-swap-oob="delete"></div>'
        )
        # Advance to first remaining unreviewed doc in other bundles.
        first_doc_id = None
        for b in bundles:
            for d in b.documents:
                if d.needs_review or d.case_id == "_TRIAGE":
                    first_doc_id = d.id
                    break
            if first_doc_id:
                break
        if first_doc_id:
            trigger["triage:advance"] = {"next_doc_id": first_doc_id, "scroll": False}
        else:
            trigger["triage:clear"] = {}

    response = HTMLResponse(content="".join(oob_parts))
    response.headers["HX-Trigger"] = json.dumps(trigger)
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
    all_cases = (
        db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    )
    proceedings = db.query(Proceeding).order_by(Proceeding.court_name.asc()).all()

    response = templates.TemplateResponse(
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

    # Auto-advance to the first remaining unconfirmed doc
    first_doc_id = None
    if bundles:
        for b in bundles:
            for d in b.documents:
                if d.needs_review or d.case_id == "_TRIAGE":
                    first_doc_id = d.id
                    break
            if first_doc_id:
                break

    if first_doc_id:
        response.headers["HX-Trigger"] = json.dumps(
            {"triage:advance": {"next_doc_id": first_doc_id}}
        )
    else:
        response.headers["HX-Trigger"] = json.dumps({"triage:clear": {}})

    return response


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
    from app.tasks.enrich_document import enrich_document_task

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    try:
        # Phase 1: metadata extraction (sender, date, originator, internal_id)
        _summarize_document_sync(doc_id, db)
        # Phase 4: management summary bullets + key passages
        enrich_document_task.delay(doc_id)
    except Exception as exc:
        doc.ai_summary_status = "failed"
        doc.ai_summary = {"error": str(exc)}
        db.commit()

    db.refresh(doc)
    return await _render_document_hud(request, doc, db)


@router.post("/triage/document/{doc_id}/retry-ai")
async def retry_ai(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """Clear AI status and re-enqueue processing task."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    # Reset statuses
    doc.ai_summary_status = "pending"
    doc.ai_summary = None
    doc.ingest_status = "pending"
    db.commit()

    from app.tasks.document_processing import process_document_task

    process_document_task.delay(doc.id)

    # Return the HUD (will show as "pending")
    return await _render_document_hud(request, doc, db)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _render_document_hud(
    request: Request, doc: Document, db: Session
) -> HTMLResponse:
    """Render the embedded HUD — reused by reingest/summarize/approve-summary/retry-ai."""
    triage_service = TriageService(db)
    cases = db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    ctx = build_triage_hud_context(db, doc, cases=cases, OriginatorType=OriginatorType)
    response = templates.TemplateResponse(request, "partials/hud/_container.html", ctx)
    # Update the card via targeted OOB (reingest/summarize/approve may change pipeline status)
    targeted_oob = _render_doc_targeted_oob(request, doc, triage_service, db)
    response.body += targeted_oob.encode("utf-8")
    return response


# -----------------------------------------------------------------------------
# Relationship suggestions (confirm / reject)
# -----------------------------------------------------------------------------


@router.post("/triage/relationship/{rel_id}/confirm")
async def confirm_relationship(
    request: Request,
    rel_id: int,
    db: Session = Depends(get_db),
):
    """Promote an AI-detected relationship to user-confirmed."""
    from app.models.enums import RelationshipConfidence

    rel = (
        db.query(DocumentRelationship).filter(DocumentRelationship.id == rel_id).first()
    )
    if not rel:
        raise HTTPException(status_code=404, detail=f"Relationship {rel_id} not found")

    rel.confidence = RelationshipConfidence.USER_CONFIRMED
    db.commit()
    return HTMLResponse("")


@router.delete("/triage/relationship/{rel_id}")
async def reject_relationship(
    request: Request,
    rel_id: int,
    db: Session = Depends(get_db),
):
    """Remove an AI-detected relationship suggestion."""
    rel = (
        db.query(DocumentRelationship).filter(DocumentRelationship.id == rel_id).first()
    )
    if not rel:
        raise HTTPException(status_code=404, detail=f"Relationship {rel_id} not found")

    db.delete(rel)
    db.commit()
    return HTMLResponse("")


# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _render_bundle_group_oob(
    request: Request, bundle, triage_service: TriageService
) -> str:
    """Render one bundle group as an OOB swap fragment.

    Replaces the entire bundle group (header + cards + footer) in-place without
    touching the rest of the feed — preserves scroll position and Alpine state.
    """
    reactions_by_doc = {
        doc.id: {r.reaction for r in triage_service.get_reactions(doc.id)}
        for doc in bundle.documents
    }
    return templates.get_template("partials/triage_bundle.html").render(
        {
            "request": request,
            "bundle": bundle,
            "reactions_by_doc": reactions_by_doc,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
            "as_oob": True,
        }
    )


def _render_triage_feed_oob(
    request: Request, triage_service: TriageService, db: Session
) -> str:
    """Renders the full triage feed as an OOB swap (used by bundle confirms)."""
    from app.models.database import Proceeding

    bundles = triage_service.get_triage_bundles()
    reactions_by_doc = {}
    for bundle in bundles:
        for doc in bundle.documents:
            reactions_by_doc[doc.id] = {
                r.reaction for r in triage_service.get_reactions(doc.id)
            }
    all_cases = (
        db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
    )
    proceedings = db.query(Proceeding).order_by(Proceeding.court_name.asc()).all()

    # Pass as_oob=True so the template adds hx-swap-oob="true" to the outer div,
    # avoiding the nested duplicate-ID problem that breaks HTMX targeting.
    return templates.get_template("partials/triage_feed.html").render(
        {
            "request": request,
            "bundles": bundles,
            "cases": all_cases,
            "proceedings": proceedings,
            "reactions_by_doc": reactions_by_doc,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
            "as_oob": True,
        }
    )


def _render_doc_targeted_oob(
    request: Request, doc, triage_service: TriageService, db: Session
) -> str:
    """Targeted OOB for a single doc confirm: updates just the card + bundle footer + badge.

    Avoids the full feed replacement that causes flicker, scroll reset, and Alpine state loss.
    """

    bundles = triage_service.get_triage_bundles()
    bundle = next(
        (b for b in bundles if any(d.id == doc.id for d in b.documents)), None
    )

    reactions_by_doc = {
        doc.id: {r.reaction for r in triage_service.get_reactions(doc.id)}
    }
    stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
    stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

    # 1. Updated card (hx_swap_oob=True adds hx-swap-oob="true" to the card div)
    card_html = templates.get_template("partials/triage_card.html").render(
        {
            "request": request,
            "doc": doc,
            "stripe_color": stripe_color,
            "stripe_icon": stripe_icon,
            "bundle": bundle,
            "reactions_by_doc": reactions_by_doc,
            "UserReactionType": UserReactionType,
            "OriginatorType": OriginatorType,
            "hx_swap_oob": True,
        }
    )

    if not bundle:
        return card_html

    # 2. Bundle footer (as_oob=True)
    footer_html = templates.get_template("partials/triage_bundle_footer.html").render(
        {
            "request": request,
            "bundle": bundle,
            "as_oob": True,
        }
    )

    # 3. Bundle badge (the ⚠ N indicator in the header row)
    if bundle.needs_review_count > 0:
        badge_inner = (
            f'<span class="inline-flex items-center gap-1 text-[9px] font-bold uppercase '
            f'tracking-wider px-1.5 py-0.5 rounded-full bg-warning-container/20 text-warning">'
            f"⚠ {bundle.needs_review_count}</span>"
        )
    else:
        badge_inner = ""
    badge_html = (
        f'<span id="triage-bundle-badge-{bundle.key}" hx-swap-oob="true">'
        f"{badge_inner}</span>"
    )

    # 4. Case chip in the bundle header (updates when a per-doc confirm sets case_id)
    if bundle.confirmed_case_id:
        chip_inner = (
            f'<span class="inline-flex items-center gap-0.5 text-[9px] font-bold font-mono '
            f"uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-primary/15 text-primary "
            f'border border-primary/30" title="Case confirmed">'
            f"{bundle.confirmed_case_id}</span>"
        )
    elif bundle.suggested_case_id:
        chip_inner = (
            f'<span class="inline-flex items-center gap-0.5 text-[9px] font-bold font-mono '
            f"uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-secondary/10 text-secondary "
            f'border border-secondary/20" title="AI-suggested case — not yet confirmed">'
            f'{bundle.suggested_case_id}<span class="opacity-60">?</span></span>'
        )
    else:
        chip_inner = (
            '<span class="inline-flex items-center text-[9px] font-bold font-mono '
            "uppercase tracking-wider px-1.5 py-0.5 rounded-full "
            'bg-surface-container-highest text-outline border border-outline/20" '
            'title="No case detected yet">?</span>'
        )
    chip_html = (
        f'<span id="triage-bundle-case-chip-{bundle.key}" hx-swap-oob="true">'
        f"{chip_inner}</span>"
    )

    return card_html + footer_html + badge_html + chip_html
