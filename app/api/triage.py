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
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    UserReactionType,
)
from app.services.hud_context import build_hud_context
from app.services.triage_service import BundleView, TriageService

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
        db.query(Case)
        .filter(Case.id != "_TRIAGE", Case.is_draft.is_(False))
        .order_by(Case.title.asc())
        .all()
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
    internal_id: str | None = Form(None),
    received_date: str | None = Form(None),
    issued_date: str | None = Form(None),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
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

    cases = (
        db.query(Case)
        .filter(Case.id != "_TRIAGE", Case.is_draft.is_(False))
        .order_by(Case.title.asc())
        .all()
    )
    ctx = build_hud_context(db, doc, mode="review", context="embedded", cases=cases)
    response = templates.TemplateResponse(request, "partials/hud/_container.html", ctx)
    # Targeted OOB: update only the affected card + bundle footer + badge.
    targeted_oob = _render_doc_targeted_oob(request, doc, triage_service, db)
    # Global OOB: sidebar badges + status bar
    global_oob = _render_sidebar_badges_oob(db)
    global_oob += _render_triage_status_bar_oob(request, triage_service)

    response.body += (targeted_oob + global_oob).encode("utf-8")

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

    # Global OOB: sidebar badges + status bar
    oob_parts.append(_render_sidebar_badges_oob(db))
    oob_parts.append(_render_triage_status_bar_oob(request, triage_service))

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
        batch_id, case_id=case_id, proceeding_id=parsed_proceeding_id, finalize=True
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
        db.query(Case)
        .filter(Case.id != "_TRIAGE", Case.is_draft.is_(False))
        .order_by(Case.title.asc())
        .all()
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
def reingest_document(
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
    return _render_document_hud(request, doc, db)


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
        doc.ai_summary = {"error": str(exc)}
        db.commit()

    db.refresh(doc)
    return _render_document_hud(request, doc, db)


@router.post("/triage/document/{doc_id}/retry-ai")
async def retry_ai(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
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
    return _render_document_hud(request, doc, db)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _render_document_hud(request: Request, doc: Document, db: Session) -> HTMLResponse:
    """Render the embedded HUD — reused by reingest/summarize/approve-summary/retry-ai."""
    triage_service = TriageService(db)
    cases = (
        db.query(Case)
        .filter(Case.id != "_TRIAGE", Case.is_draft.is_(False))
        .order_by(Case.title.asc())
        .all()
    )
    ctx = build_hud_context(db, doc, mode="review", context="embedded", cases=cases)
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
# Card live-update endpoint (polled by self-disarming probe in triage_card.html)
# -----------------------------------------------------------------------------


@router.get("/triage/card/{doc_id}/live")
async def triage_card_live(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """Return OOB card+footer+badge+case-chip for a single doc (polling refresh)."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse("", status_code=404)

    triage_service = TriageService(db)
    return HTMLResponse(
        _render_doc_targeted_oob(request, doc, triage_service, db, allow_delete=False)
    )


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
        "partials/triage_bundle.html",
        {
            "bundle": bundle,
            "reactions_by_doc": reactions_by_doc,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
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
    import json
    from types import SimpleNamespace

    from sqlalchemy import text

    from app.services.pipeline_status import aggregate_pipeline_summary

    rows = db.execute(
        text("SELECT pipeline_stages FROM documents WHERE ingest_batch_id = :b"),
        {"b": batch_id},
    ).fetchall()
    if not rows:
        return HTMLResponse("", status_code=404)

    stages_per_doc = [json.loads(r[0] or "{}") for r in rows]
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
        db.query(Case)
        .filter(Case.id != "_TRIAGE", Case.is_draft.is_(False))
        .order_by(Case.title.asc())
        .all()
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
    request: Request,
    doc,
    triage_service: TriageService,
    db: Session,
    allow_delete: bool = True,
) -> str:
    """Targeted OOB for a single doc confirm: updates just the card + bundle footer + badge.

    Avoids the full feed replacement that causes flicker, scroll reset, and Alpine state loss.
    Returns a delete swap if the document is no longer in the triage bundles, unless
    allow_delete=False (used by the passive 4 s polling probe so the queue stays visible
    until the user explicitly acts or refreshes the page).
    """

    # 1. Determine if the document should be in triage at all.
    # Logic matches TriageService.get_triage_bundles() filter.
    in_triage_via_case = doc.case_id == "_TRIAGE" or doc.needs_review
    in_triage_via_batch = False
    if doc.ingest_batch_id:
        # A batch is in triage if its status is NOT completed or awaiting_slicing.
        batch = doc.ingest_batch
        if batch and batch.status not in (
            IngestBatchStatus.COMPLETED,
            IngestBatchStatus.AWAITING_SLICING,
        ):
            in_triage_via_batch = True

    should_delete = not in_triage_via_case and not in_triage_via_batch
    if should_delete and allow_delete:
        return f'<div id="triage-card-{doc.id}" hx-swap-oob="delete"></div>'

    # 2. Fetch or construct the BundleView for this document.
    # Using specific fetch/construction instead of get_triage_bundles() prevents
    # documents disappearing due to pagination limits.
    bundle = None
    if doc.ingest_batch_id:
        bundle = triage_service.get_bundle_by_batch_id(doc.ingest_batch_id)
        if bundle and not in_triage_via_batch and allow_delete:
            # If the batch itself is COMPLETED, the bundle in the UI should only
            # show documents that specifically need review (in_triage_via_case).
            # Skipped when allow_delete=False so the card stays rendered and the
            # polling probe can disarm naturally once all pipeline stages are terminal.
            bundle.documents = [
                d for d in bundle.documents if d.case_id == "_TRIAGE" or d.needs_review
            ]
            triage_service._enrich_bundle(bundle)
    else:
        # Synthetic bundle for loose (pre-IngestBatch) document
        bundle = BundleView(
            key=f"loose-{doc.id}",
            batch_id=None,
            source_type=IngestBatchSourceType.MANUAL,
            subject=doc.title,
            sender_email=None,
            received_at=doc.ingest_date or datetime.now(),
            confirmed_case_id=doc.case_id if doc.case_id != "_TRIAGE" else None,
            proceeding=doc.proceeding,
            documents=[doc],
        )
        triage_service._enrich_bundle(bundle)

    if not bundle or not any(d.id == doc.id for d in bundle.documents):
        if not allow_delete:
            return ""
        return f'<div id="triage-card-{doc.id}" hx-swap-oob="delete"></div>'

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


def _render_sidebar_badges_oob(db: Session) -> str:
    """Render global sidebar badges (triage, notifications) as OOB swaps."""
    from app.helpers import _build_notifications, build_sidebar_counts

    counts = build_sidebar_counts(db)
    notif_data = _build_notifications(db)
    notif_count = notif_data["notification_count"]

    # Triage Badge
    triage_badge_inner = ""
    if counts["triage_count"] > 0:
        triage_badge_inner = (
            f'<span class="absolute -top-1 -right-1 flex items-center justify-center min-w-[16px] h-4 px-1 bg-error text-surface text-[9px] font-bold rounded-full border-2 border-surface-container-low">'
            f"{counts['triage_count']}</span>"
        )
    triage_oob = f'<div id="sidebar-triage-badge-container" hx-swap-oob="true">{triage_badge_inner}</div>'

    # Notifications Badge
    notif_badge_inner = ""
    if notif_count > 0:
        notif_badge_inner = (
            f'<span class="absolute -top-1 -right-1 flex items-center justify-center min-w-[16px] h-4 px-1 bg-error text-surface text-[9px] font-bold rounded-full border-2 border-surface-container-low">'
            f"{notif_count}</span>"
        )
    notif_oob = f'<div id="sidebar-notifications-badge-container" hx-swap-oob="true">{notif_badge_inner}</div>'

    return triage_oob + notif_oob


def _render_triage_status_bar_oob(
    request: Request, triage_service: TriageService
) -> str:
    """Render the triage status bar (with counts) as an OOB swap."""
    bundles = triage_service.get_triage_bundles()
    total_docs = sum(len(b.documents) for b in bundles)
    counts = {
        "court": 0,
        "opposing": 0,
        "own": 0,
        "third_party": 0,
        "unknown": 0,
        "bundles": len(bundles),
    }
    for b in bundles:
        for doc in b.documents:
            if doc.originator_type == OriginatorType.COURT:
                counts["court"] += 1
            elif doc.originator_type == OriginatorType.OPPOSING:
                counts["opposing"] += 1
            elif doc.originator_type == OriginatorType.OWN:
                counts["own"] += 1
            elif doc.originator_type == OriginatorType.THIRD_PARTY:
                counts["third_party"] += 1
            else:
                counts["unknown"] += 1

    return templates.get_template("partials/triage_status_bar.html").render(
        {
            "request": request,
            "counts_by_type": counts,
            "total_docs": total_docs,
            "as_oob": True,
        }
    )
