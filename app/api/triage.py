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
from app.models.enums import (
    OriginatorType,
    UserReactionType,
)
from app.repositories.case import CaseRepository
from app.services.hud_context import build_hud_context
from app.services.triage_service import TriageService
from app.services.triage_view import (
    failed_doc_summary,
    render_bundle_group_oob,
    render_row_targeted_oob,
    render_sidebar_badges_oob,
    render_triage_header_stats_oob,
)

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
    all_cases = CaseRepository(db).list_for_picker()
    total_docs = sum(b.doc_count for b in bundles)

    reactions_by_doc = {}
    for bundle in bundles:
        for doc in bundle.documents:
            reactions_by_doc[doc.id] = {
                r.reaction for r in triage_service.get_reactions(doc.id)
            }  # set for card-level membership check only

    proceedings = db.query(Proceeding).order_by(Proceeding.court_name.asc()).all()

    drafts_pending = db.query(Case).filter(Case.is_draft.is_(True)).count()
    first_draft_doc_id = None
    if drafts_pending:
        _row = (
            db.query(Document.id)
            .join(Case, Case.id == Document.case_id)
            .filter(Case.is_draft.is_(True))
            .order_by(Document.id.asc())
            .first()
        )
        if _row:
            first_draft_doc_id = _row[0]

    failed_count, first_failed_doc_id = failed_doc_summary(bundles)

    from app.services.triage_view import stats_for_chips

    header_stats = stats_for_chips(bundles)
    sub_bundles_by_key = {b.key: b.sub_bundles for b in bundles}
    mock_status_by_key = {b.key: b.mock_status for b in bundles}

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
        drafts_pending=drafts_pending,
        first_draft_doc_id=first_draft_doc_id,
        failed_count=failed_count,
        first_failed_doc_id=first_failed_doc_id,
        reactions_by_doc=reactions_by_doc,
        header_stats=header_stats,
        sub_bundles_by_key=sub_bundles_by_key,
        mock_status_by_key=mock_status_by_key,
        limit=limit,
        offset=offset,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        OriginatorType=OriginatorType,
        UserReactionType=UserReactionType,
    )


# -----------------------------------------------------------------------------
# Dismiss bundle (POST)
# -----------------------------------------------------------------------------


@router.post("/triage/dismiss")
async def dismiss_bundle(
    batch_id: int | None = None,
    doc_id: int | None = None,
    db: Session = Depends(get_db),
    service: TriageService = Depends(get_triage_service),
):
    success = service.dismiss_bundle(batch_id=batch_id, doc_id=doc_id)
    if not success:
        raise HTTPException(status_code=404, detail="Bundle or document not found")

    # Return OOB swap to delete the row
    target_id = (
        f"triage-row-batch-{batch_id}" if batch_id else f"triage-row-doc-{doc_id}"
    )
    html = f'<div id="{target_id}" hx-swap-oob="delete"></div>'

    # Check if triage is now empty and return empty state if so
    bundles = service.get_triage_bundles(limit=1)
    if not bundles:
        # Re-render the triage feed with empty state
        return templates.TemplateResponse(
            "partials/triage_feed.html",
            {"request": {}, "bundles": [], "hx_swap_oob": "true"},
            headers={"HX-Reswap": "innerHTML", "HX-Target": "#triage-feed"},
        )

    return HTMLResponse(content=html)


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
    internal_id: str | None = Form(None),
    received_date: str | None = Form(None),
    issued_date: str | None = Form(None),
    significance_tier: str | None = Form(None),
    document_type: str | None = Form(None),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    from app.models.enums import DocumentType, SignificanceTier

    resolved_case_id = case_id if case_id else None

    pre_confirm_doc = db.query(Document).filter(Document.id == doc_id).first()
    pre_confirm_case_id = pre_confirm_doc.case_id if pre_confirm_doc else None

    parsed_originator = None
    if originator_type:
        try:
            parsed_originator = OriginatorType(originator_type)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Unknown originator: {originator_type}"
            ) from exc

    parsed_significance = None
    if significance_tier:
        try:
            parsed_significance = SignificanceTier(significance_tier)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown significance tier: {significance_tier}",
            ) from exc

    parsed_document_type = None
    if document_type:
        try:
            parsed_document_type = DocumentType(document_type)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Unknown document type: {document_type}"
            ) from exc

    parsed_issued_date = None
    if issued_date:
        try:
            parsed_issued_date = datetime.strptime(issued_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid date: {issued_date}"
            ) from exc

    parsed_received_date = None
    if received_date:
        try:
            parsed_received_date = datetime.strptime(received_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid date: {received_date}"
            ) from exc

    doc = triage_service.confirm_document(
        doc_id,
        title=title,
        case_id=resolved_case_id,
        originator_type=parsed_originator,
        sender=sender,
        internal_id=internal_id if internal_id else None,
        issued_date=parsed_issued_date,
        received_date=parsed_received_date,
        significance_tier=parsed_significance,
        document_type=parsed_document_type,
        finalize=True,
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    # If this confirm moved the doc out of _TRIAGE, re-trigger downstream enrichment.
    if (
        (not pre_confirm_case_id or pre_confirm_case_id == "_TRIAGE")
        and doc.case_id
        and doc.case_id != "_TRIAGE"
    ):
        from app.services.triage_service import _reset_and_reenrich

        _reset_and_reenrich(db, [doc])

    cases = CaseRepository(db).list_for_picker()
    ctx = build_hud_context(db, doc, mode="review", context="embedded", cases=cases)
    response = templates.TemplateResponse(request, "partials/triage/_doc_hud.html", ctx)
    # Targeted OOB: update only the affected card + bundle footer + badge.
    targeted_oob = render_row_targeted_oob(request, doc, triage_service, db)
    # Global OOB: sidebar badges + status bar
    global_oob = render_sidebar_badges_oob(db)
    global_oob += render_triage_header_stats_oob(request, triage_service)

    response.body += (targeted_oob + global_oob).encode("utf-8")

    # Confirm-and-next: if the doc is now out of triage, tell the client which
    # doc to advance to. Alpine listener picks this up from the HX-Trigger
    # header and shifts focus.
    if not doc.needs_review and doc.case_id and doc.case_id != "_TRIAGE":
        trigger: dict = {}
        next_doc = triage_service.find_next_review_doc(doc.id)
        if next_doc:
            trigger["triage:advance"] = {"next_doc_id": next_doc.id}
        else:
            trigger["triage:clear"] = {}
        # Surface destination so the page can show a toast linking to the case.
        case_obj = db.query(Case).filter(Case.id == doc.case_id).first()
        trigger["case:confirmed"] = {
            "case_id": doc.case_id,
            "case_title": case_obj.title if case_obj else "",
            "doc_count": 1,
            "action": "assigned",
        }
        response.headers["HX-Trigger"] = json.dumps(trigger)

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
    case_id: str | None = Form(None),
    new_case_id: str | None = Form(None),
    new_case_title: str | None = Form(None),
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
    from app.services.case_service import get_or_create_case_from_reference

    # If user chose to create a new case — use the full helper so a Proceeding is also created
    if new_case_id:
        batch_subj = new_case_title or None
        new_case_obj, _, _ = get_or_create_case_from_reference(
            db,
            internal_id=new_case_id,
            batch_subject=batch_subj,
            is_draft=False,
        )
        db.flush()
        case_id = new_case_obj.id

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
    from app.services.triage_service import _reset_and_reenrich

    if is_synthetic == "true" and doc_id:
        _doc_id = int(doc_id)
        bundle_key = f"loose-{_doc_id}"
        pre_case = db.query(Document.case_id).filter(Document.id == _doc_id).scalar()
        updated_doc = triage_service.confirm_document(
            _doc_id, case_id=case_id, finalize=finalize
        )
        if not updated_doc:
            raise HTTPException(status_code=404, detail=f"Document {_doc_id} not found")
        if parsed_proceeding_id is not None:
            updated_doc.proceeding_id = parsed_proceeding_id
            db.commit()
            db.refresh(updated_doc)
        if (not pre_case or pre_case == "_TRIAGE") and case_id and case_id != "_TRIAGE":
            _reset_and_reenrich(db, [updated_doc])
    else:
        if not batch_id:
            raise HTTPException(
                status_code=422, detail="batch_id is required for bundle confirm"
            )
        _batch_id = int(batch_id)
        bundle_key = f"batch-{_batch_id}"
        # Capture which docs are still _TRIAGE before the cascade.
        pre_triage_docs = (
            db.query(Document)
            .filter(
                Document.ingest_batch_id == _batch_id,
                Document.case_id == "_TRIAGE",
            )
            .all()
        )
        batch = triage_service.confirm_bundle(
            _batch_id,
            case_id=case_id,
            proceeding_id=parsed_proceeding_id,
            finalize=finalize,
        )
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {_batch_id} not found")
        if case_id and case_id != "_TRIAGE" and pre_triage_docs:
            for d in pre_triage_docs:
                db.refresh(d)
            _reset_and_reenrich(db, pre_triage_docs)

    # ---- build targeted OOB response (no full feed replacement) ----
    bundles = triage_service.get_triage_bundles()
    updated_bundle = next((b for b in bundles if b.key == bundle_key), None)

    oob_parts: list[str] = []
    trigger: dict = {"triage:bundle-confirmed": {}}

    if updated_bundle:
        # Bundle still in triage — OOB-swap the whole bundle group (updates
        # case chip in header, all cards, footer, badge in one shot).
        oob_parts.append(
            render_bundle_group_oob(request, updated_bundle, triage_service)
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
            f'<div id="triage-row-{bundle_key}" hx-swap-oob="delete"></div>'
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

    # Global OOB: sidebar badges + status bar
    oob_parts.append(render_sidebar_badges_oob(db))
    oob_parts.append(render_triage_header_stats_oob(request, triage_service))

    # Surface destination so the page can show a clickable toast.
    if case_id and case_id != "_TRIAGE":
        case_obj = db.query(Case).filter(Case.id == case_id).first()
        # Doc count is whatever just got cascaded — for synthetic single-doc
        # bundles that's 1, otherwise count the docs now living on this case
        # within the batch.
        if is_synthetic == "true":
            cascaded_count = 1
        elif batch_id:
            cascaded_count = (
                db.query(Document)
                .filter(
                    Document.ingest_batch_id == int(batch_id),
                    Document.case_id == case_id,
                )
                .count()
            )
        else:
            cascaded_count = 0
        trigger["case:confirmed"] = {
            "case_id": case_id,
            "case_title": case_obj.title if case_obj else "",
            "doc_count": cascaded_count,
            "action": "created" if new_case_id else "assigned",
        }

    response = HTMLResponse(content="".join(oob_parts))
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


# -----------------------------------------------------------------------------
# Document actions (reingest, summarize, approve-summary)
# -----------------------------------------------------------------------------


@router.post("/document/{doc_id}/reingest")
def reingest_document(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
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
    return _render_document_hud(request, doc, db, triage_service)


@router.post("/document/{doc_id}/summarize")
async def summarize_document(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
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
        doc.ai_summary = {"error": str(exc)}
        db.commit()

    db.refresh(doc)
    return _render_document_hud(request, doc, db, triage_service)


@router.post("/triage/document/{doc_id}/retry-ai")
async def retry_ai(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Re-enqueue enrichment for a document (forwards to per-stage retry)."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    from app.models.enums import PipelineStage
    from app.services.pipeline_status import reset_stage
    from app.tasks.document_processing import process_document_task

    reset_stage(doc.id, PipelineStage.EXTRACT, db)
    process_document_task.delay(doc.id)

    db.refresh(doc)
    return _render_document_hud(request, doc, db, triage_service)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _render_document_hud(
    request: Request,
    doc: Document,
    db: Session,
    triage_service: TriageService,
) -> HTMLResponse:
    """Render the triage doc HUD — reused by reingest/summarize/approve-summary/retry-ai.

    `triage_service` is passed in (not constructed) so callers go through the
    same `Depends(get_triage_service)` DI as their routes — keeps the service's
    dependency lifetime under the framework's control.
    """
    cases = CaseRepository(db).list_for_picker()
    ctx = build_hud_context(db, doc, mode="review", context="embedded", cases=cases)
    response = templates.TemplateResponse(request, "partials/triage/_doc_hud.html", ctx)
    # Update the card via targeted OOB (reingest/summarize/approve may change pipeline status)
    targeted_oob = render_row_targeted_oob(request, doc, triage_service, db)
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
    """Promote an AI-detected relationship to user-confirmed, closing the target's thread."""
    from app.models.enums import RelationshipConfidence
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    rel = (
        db.query(DocumentRelationship).filter(DocumentRelationship.id == rel_id).first()
    )
    if not rel:
        raise HTTPException(status_code=404, detail=f"Relationship {rel_id} not found")

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
):
    """Remove a relationship suggestion, reopening the target's thread if no confirmed edges remain."""
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    rel = (
        db.query(DocumentRelationship).filter(DocumentRelationship.id == rel_id).first()
    )
    if not rel:
        raise HTTPException(status_code=404, detail=f"Relationship {rel_id} not found")

    target_id = rel.to_document_id
    source_id = rel.from_document_id
    db.delete(rel)
    db.commit()
    recompute_thread_open(target_id, db)
    source_doc = db.query(Document).filter(Document.id == source_id).first()
    if source_doc:
        refresh_review_reasons(source_doc, db)
    return HTMLResponse("")


# -----------------------------------------------------------------------------
# Card live-update endpoint (polled by self-disarming probe in the bundle row)
# -----------------------------------------------------------------------------


@router.get("/triage/card/{doc_id}/live")
async def triage_card_live(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Return OOB row swap for a single doc (polling refresh).

    The row aggregates the doc's bundle; the new triage row template owns its own
    polling probe scoped per bundle, but this endpoint is still used by direct
    consumers (e.g., chunked retries that target a single doc).
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse("", status_code=404)

    return HTMLResponse(
        render_row_targeted_oob(request, doc, triage_service, db, allow_delete=False)
    )


# -----------------------------------------------------------------------------
# Triage-shaped per-doc HUD partial — the inline expand and drawer body fetch
# this instead of /document/{id}?context=triage so the case-dashboard HUD stays
# untouched while triage gets its own composition.
# -----------------------------------------------------------------------------


@router.post("/triage/document/{doc_id}/title")
async def update_doc_title(
    doc_id: int,
    title: str = Form(""),
    db: Session = Depends(get_db),
):
    """Inline title patch from the doc-HUD header.

    Updates only `doc.title`. Does not finalize / clear `needs_review`. Empty /
    whitespace-only `title` is a no-op (we keep the existing AI title rather
    than wiping it). Returns 204 — caller uses `hx-swap="none"`.
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    new_title = (title or "").strip()
    if new_title:
        doc.title = new_title
        db.commit()
    return HTMLResponse("", status_code=204)


@router.get("/triage/doc/{doc_id}/hud")
async def triage_doc_hud(
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
async def triage_doc_body(
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


# -----------------------------------------------------------------------------
@router.get("/triage/bundle/{batch_id}")
async def get_bundle(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Return the rendered HTML for a single bundle group (no OOB)."""
    bundle = triage_service.get_bundle_by_batch_id(batch_id)
    if not bundle:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

    reactions_by_doc = {
        doc.id: {r.reaction for r in triage_service.get_reactions(doc.id)}
        for doc in bundle.documents
    }

    return templates.TemplateResponse(
        request,
        "partials/triage_row.html",
        {
            "bundle": bundle,
            "reactions_by_doc": reactions_by_doc,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "ORIGINATOR_COLORS": ORIGINATOR_COLORS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
        },
    )


# Bundle pipeline aggregate endpoint
# -----------------------------------------------------------------------------


@router.get("/triage/bundle/{batch_id}/pipeline")
async def bundle_pipeline_status(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
):
    """Return pipeline aggregate chip for a bundle (triage bundle header polling).

    Uses a focused single-table query — does not rebuild the full triage feed.
    """
    from types import SimpleNamespace

    from app.repositories.document import DocumentRepository
    from app.services.pipeline_status import aggregate_pipeline_summary

    stages_per_doc = DocumentRepository(db).get_pipeline_stages_for_batch(batch_id)
    if not stages_per_doc:
        return HTMLResponse("", status_code=404)

    summary = aggregate_pipeline_summary(stages_per_doc)

    n_total = summary.get("total", 0)
    n_done = (
        summary.get("completed", 0)
        + summary.get("failed", 0)
        + summary.get("skipped", 0)
    )

    _TERMINAL = {"completed", "failed", "skipped"}
    batch_analysis_terminal = bool(stages_per_doc) and all(
        (d.get("batch_analysis", {}) or {}).get("status") in _TERMINAL
        for d in stages_per_doc
    )

    # Minimal stub — template only needs .pipeline_summary, .key, .batch_id
    bundle_stub = SimpleNamespace(
        batch_id=batch_id,
        key=f"batch-{batch_id}",
        pipeline_summary=summary,
    )

    response = templates.TemplateResponse(
        request,
        "partials/_pipeline_aggregate.html",
        {"bundle": bundle_stub},
    )

    # Bundle re-render fires on two distinct cues so parent/child relationships
    # (set by BATCH_ANALYSIS) become visible without a manual refresh:
    #   1. BATCH_ANALYSIS terminal across the batch — refresh once, latch via
    #      IngestBatch.meta so subsequent polls don't refire while later stages
    #      still run.
    #   2. All stages terminal — final consolidation refresh.
    fire_reload = n_total > 0 and n_done == n_total

    if batch_analysis_terminal and not fire_reload:
        from app.models.database import IngestBatch

        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if batch is not None:
            meta = dict(batch.meta or {})
            if not meta.get("batch_analysis_reload_fired"):
                meta["batch_analysis_reload_fired"] = True
                batch.meta = meta
                db.commit()
                fire_reload = True

    if fire_reload:
        import time

        response.headers["HX-Trigger"] = json.dumps(
            {f"reload-bundle-{batch_id}": {"ts": time.time()}}
        )

    return response
