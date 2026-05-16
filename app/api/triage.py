import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

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


def _render_picker(
    request: Request, batch_id: int, triage_service: TriageService
) -> str:
    """Re-fetch bundle and render triage_doc_tree_picker.html for HTMX outerHTML swap."""
    bundle = triage_service.get_bundle_by_batch_id(batch_id)
    if not bundle:
        return "<div>Bundle not found</div>"

    return templates.get_template("partials/triage_doc_tree_picker.html").render(
        {
            "request": request,
            "bundle": bundle,
            "active_doc_id": None,
            "compact": True,
        }
    )


# -----------------------------------------------------------------------------
# Triage page (GET)
# -----------------------------------------------------------------------------


@router.get("/triage")
def triage_page(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    sort: str = "received",
    dir: str = "desc",
    case_id: list[str] = Query(default=[]),
    proceeding_id: list[str] = Query(default=[]),
    pipeline_filter: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    from app.models.database import Proceeding

    filter_options = triage_service.get_triage_filter_options()

    bundles = triage_service.get_triage_bundles(
        limit=limit,
        offset=offset,
        sort=sort,
        direction=dir,
        case_ids=case_id,
        proceeding_ids=proceeding_id,
        pipeline_filters=pipeline_filter,
    )
    slicing_queue = triage_service.get_slicing_queue()
    all_cases = CaseRepository(db).list_for_picker()
    total_docs = sum(b.doc_count for b in bundles)

    all_doc_ids = [doc.id for bundle in bundles for doc in bundle.documents]
    reactions_by_doc = triage_service.get_reactions_by_doc_ids(all_doc_ids)

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
        sort=sort,
        dir=dir,
        case_ids=case_id,
        proceeding_ids=proceeding_id,
        pipeline_filters=pipeline_filter,
        case_options=filter_options["case_options"],
        proceeding_options=filter_options["proceeding_options"],
        pipeline_options=filter_options["pipeline_options"],
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        OriginatorType=OriginatorType,
        UserReactionType=UserReactionType,
    )


# -----------------------------------------------------------------------------
# Triage feed partial (HTMX)
# -----------------------------------------------------------------------------


@router.get("/triage/feed")
def triage_feed_partial(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    sort: str = "received",
    dir: str = "desc",
    case_id: list[str] = Query(default=[]),
    proceeding_id: list[str] = Query(default=[]),
    pipeline_filter: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    from app.services.triage_view import stats_for_chips

    bundles = triage_service.get_triage_bundles(
        limit=limit,
        offset=offset,
        sort=sort,
        direction=dir,
        case_ids=case_id,
        proceeding_ids=proceeding_id,
        pipeline_filters=pipeline_filter,
    )
    all_doc_ids = [doc.id for bundle in bundles for doc in bundle.documents]
    reactions_by_doc = triage_service.get_reactions_by_doc_ids(all_doc_ids)
    header_stats = stats_for_chips(bundles)
    sub_bundles_by_key = {b.key: b.sub_bundles for b in bundles}
    mock_status_by_key = {b.key: b.mock_status for b in bundles}

    feed_html = templates.get_template("partials/triage_feed.html").render(
        {
            "request": request,
            "bundles": bundles,
            "case_ids": case_id,
            "proceeding_ids": proceeding_id,
            "pipeline_filters": pipeline_filter,
            "reactions_by_doc": reactions_by_doc,
            "sub_bundles_by_key": sub_bundles_by_key,
            "mock_status_by_key": mock_status_by_key,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
        }
    )

    stats_html = templates.get_template("partials/triage_filter_chips.html").render(
        {
            "request": request,
            "header_stats": header_stats,
            "as_oob": True,
        }
    )

    return HTMLResponse(content=feed_html + stats_html)


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
        f"triage-row-batch-{batch_id}" if batch_id else f"triage-row-loose-{doc_id}"
    )
    html = f'<div id="{target_id}" hx-swap-oob="delete"></div>'

    # If triage is now empty, OOB-replace the entire feed with its empty state.
    # The feed partial's outer #triage-feed div carries hx-swap-oob="true"
    # (via as_oob=True), so HTMX outerHTML-swaps it regardless of the
    # client's hx-swap setting — without this, the deleted row's polling
    # children stay in the DOM and 404 the server every 4s.
    bundles = service.get_triage_bundles(limit=1)
    if not bundles:
        empty_feed = templates.get_template("partials/triage_feed.html").render(
            bundles=[], as_oob=True
        )
        return HTMLResponse(content=html + empty_feed)

    return HTMLResponse(content=html)


# -----------------------------------------------------------------------------
# Delete bundle (POST) — hard-delete, complement to /triage/dismiss
# -----------------------------------------------------------------------------


@router.post("/triage/delete")
async def delete_bundle(
    batch_id: int | None = None,
    doc_id: int | None = None,
    db: Session = Depends(get_db),
    service: TriageService = Depends(get_triage_service),
):
    try:
        success = service.delete_bundle(batch_id=batch_id, doc_id=doc_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not success:
        raise HTTPException(status_code=404, detail="Bundle or document not found")

    target_id = (
        f"triage-row-batch-{batch_id}" if batch_id else f"triage-row-loose-{doc_id}"
    )
    html = f'<div id="{target_id}" hx-swap-oob="delete"></div>'

    # If triage is now empty, OOB-replace the entire feed with its empty state.
    # See dismiss_bundle for why this beats relying on response retarget headers.
    bundles = service.get_triage_bundles(limit=1)
    if not bundles:
        empty_feed = templates.get_template("partials/triage_feed.html").render(
            bundles=[], as_oob=True
        )
        return HTMLResponse(content=html + empty_feed)

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
    pre_confirm_tier = pre_confirm_doc.significance_tier if pre_confirm_doc else None

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

    # Offer to re-run pipeline from enrich when significance tier was changed and
    # enrichment has already completed (no point prompting if pipeline is still running).
    tier_requeue_prompt = (
        parsed_significance is not None
        and pre_confirm_tier != parsed_significance
        and (doc.pipeline_stages or {}).get("enrich", {}).get("status")
        in ("completed", "skipped")
    )

    cases = CaseRepository(db).list_for_picker()
    ctx = build_hud_context(db, doc, mode="review", context="embedded", cases=cases)
    ctx["tier_requeue_prompt"] = tier_requeue_prompt
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
            # Promote draft proceeding once the user has ratified it.
            from app.models.database import Proceeding

            proc = (
                db.query(Proceeding)
                .filter(Proceeding.id == parsed_proceeding_id)
                .first()
            )
            if proc and proc.is_draft:
                proc.is_draft = False
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
# Batch operations
# -----------------------------------------------------------------------------


def _parse_bundle_key(key: str) -> tuple[int | None, int | None]:
    """Parse a bundle key into (batch_id, doc_id). Returns (None, None) on failure."""
    if key.startswith("batch-"):
        try:
            return int(key[6:]), None
        except ValueError:
            return None, None
    if key.startswith("loose-"):
        try:
            return None, int(key[6:])
        except ValueError:
            return None, None
    return None, None


@router.post("/triage/batch/confirm")
async def batch_confirm(
    request: Request,
    bundle_keys: list[str] = Form(...),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Silently confirm each selected bundle to its own AI-suggested case+proceeding.

    Bundles without a suggestion are skipped. Returns OOB swaps for all affected
    rows and fires `triage:batch-confirmed` with confirmed/skipped counts.
    """
    from app.services.triage_service import _reset_and_reenrich
    from app.services.triage_view import render_batch_oob

    confirmed_count = 0
    skipped_count = 0
    confirmed_keys: list[str] = []

    for key in bundle_keys:
        batch_id, doc_id = _parse_bundle_key(key)
        if batch_id is None and doc_id is None:
            skipped_count += 1
            continue

        case_id, proceeding_id = triage_service.get_bundle_suggestion(
            batch_id=batch_id, doc_id=doc_id
        )
        if not case_id:
            skipped_count += 1
            continue

        if batch_id:
            pre_triage_docs = (
                db.query(Document)
                .filter(
                    Document.ingest_batch_id == batch_id,
                    Document.case_id == "_TRIAGE",
                )
                .all()
            )
            batch = triage_service.confirm_bundle(
                batch_id,
                case_id=case_id,
                proceeding_id=proceeding_id,
                finalize=True,
            )
            if batch and case_id != "_TRIAGE" and pre_triage_docs:
                for d in pre_triage_docs:
                    db.refresh(d)
                _reset_and_reenrich(db, pre_triage_docs)
        else:
            pre_case = db.query(Document.case_id).filter(Document.id == doc_id).scalar()
            updated_doc = triage_service.confirm_document(
                doc_id, case_id=case_id, finalize=True
            )
            if updated_doc and proceeding_id:
                updated_doc.proceeding_id = proceeding_id
                db.commit()
                db.refresh(updated_doc)
            if (
                updated_doc
                and (not pre_case or pre_case == "_TRIAGE")
                and case_id != "_TRIAGE"
            ):
                _reset_and_reenrich(db, [updated_doc])

        confirmed_keys.append(key)
        confirmed_count += 1

    oob_html = render_batch_oob(request, bundle_keys, triage_service, db)
    trigger = {
        "triage:batch-confirmed": {
            "confirmed": confirmed_count,
            "skipped": skipped_count,
        }
    }
    response = HTMLResponse(content=oob_html)
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


@router.post("/triage/batch/assign")
async def batch_assign(
    request: Request,
    bundle_keys: list[str] = Form(...),
    case_id: str | None = Form(None),
    new_case_id: str | None = Form(None),
    new_case_title: str | None = Form(None),
    proceeding_id: str | None = Form(None),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Assign all selected bundles to a single case and optional proceeding.

    Mirrors the logic of POST /triage/confirm but applied to a list of bundle
    keys at once. Returns OOB swaps for all affected rows.
    """
    from app.services.case_service import get_or_create_case_from_reference
    from app.services.triage_service import _reset_and_reenrich
    from app.services.triage_view import render_batch_oob

    if new_case_id:
        new_case_obj, _, _ = get_or_create_case_from_reference(
            db,
            internal_id=new_case_id,
            batch_subject=new_case_title or None,
            is_draft=False,
        )
        db.flush()
        case_id = new_case_obj.id

    if not case_id:
        raise HTTPException(status_code=422, detail="case_id is required")

    parsed_proceeding_id: int | None = None
    if proceeding_id:
        try:
            parsed_proceeding_id = int(proceeding_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid proceeding_id: {proceeding_id}"
            ) from exc

    assigned_count = 0

    for key in bundle_keys:
        batch_id, doc_id = _parse_bundle_key(key)
        if batch_id is None and doc_id is None:
            continue

        if batch_id:
            pre_triage_docs = (
                db.query(Document)
                .filter(
                    Document.ingest_batch_id == batch_id,
                    Document.case_id == "_TRIAGE",
                )
                .all()
            )
            batch = triage_service.confirm_bundle(
                batch_id,
                case_id=case_id,
                proceeding_id=parsed_proceeding_id,
                finalize=False,
            )
            if batch and case_id != "_TRIAGE" and pre_triage_docs:
                for d in pre_triage_docs:
                    db.refresh(d)
                _reset_and_reenrich(db, pre_triage_docs)
        else:
            pre_case = db.query(Document.case_id).filter(Document.id == doc_id).scalar()
            updated_doc = triage_service.confirm_document(
                doc_id, case_id=case_id, finalize=False
            )
            if updated_doc and parsed_proceeding_id:
                from app.models.database import Proceeding

                updated_doc.proceeding_id = parsed_proceeding_id
                proc = (
                    db.query(Proceeding)
                    .filter(Proceeding.id == parsed_proceeding_id)
                    .first()
                )
                if proc and proc.is_draft:
                    proc.is_draft = False
                db.commit()
                db.refresh(updated_doc)
            if (
                updated_doc
                and (not pre_case or pre_case == "_TRIAGE")
                and case_id != "_TRIAGE"
            ):
                _reset_and_reenrich(db, [updated_doc])

        assigned_count += 1

    case_obj = db.query(Case).filter(Case.id == case_id).first()
    oob_html = render_batch_oob(request, bundle_keys, triage_service, db)
    trigger = {
        "triage:batch-assigned": {
            "count": assigned_count,
            "case_id": case_id,
            "case_title": case_obj.title if case_obj else "",
        },
        "triage:bundle-confirmed": {},
    }
    response = HTMLResponse(content=oob_html)
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


# -----------------------------------------------------------------------------
# Document actions (reingest, summarize, approve-summary)
# -----------------------------------------------------------------------------


@router.post("/document/{doc_id}/reingest")
async def reingest_document(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    from app.services.pipeline_status import reset_all_stages, retry_on_db_locked
    from app.tasks.dispatch import dispatch_task
    from app.tasks.document_processing import process_document_task

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    try:
        retry_on_db_locked(lambda: reset_all_stages(doc.id, db), db)
    except OperationalError as exc:
        raise HTTPException(
            status_code=409, detail="Worker busy — try again in a moment"
        ) from exc
    dispatch_task(process_document_task, doc.id)
    db.refresh(doc)
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
        from app.tasks.dispatch import dispatch_task

        dispatch_task(enrich_document_task, doc_id)
    except Exception as exc:
        doc.ai_summary = {"error": str(exc)}
        db.commit()

    db.refresh(doc)
    return _render_document_hud(request, doc, db, triage_service)


# -----------------------------------------------------------------------------
# Bundle-level retry
# -----------------------------------------------------------------------------


def _retry_batch_reset(batch, db, *, full: bool = False):
    """Reset pipeline stages for one batch without committing or dispatching.

    Returns (dispatch_items, batch_fallback) on success, or -1 if any stage is
    actively running (caller treats as skip/409). Does NOT commit — the caller
    must commit before calling _retry_batch_dispatch with the returned items.

    dispatch_items is a list of (doc_id, batch_id, head_stage | None, needs_emb).
    batch_fallback is True when no per-doc head was found (BATCH_ANALYSIS fallback).
    """
    from sqlalchemy import text as _text

    from app.models.database import Case, Proceeding
    from app.models.enums import (
        DocumentRole,
        IngestBatchStatus,
        PipelineStage,
        StageStatus,
    )
    from app.services.pipeline_status import _STAGE_ORDER, compute_overall_state

    # Re-read after any rollback so ORM state reflects DB reality.
    db.refresh(batch)

    # Bail out if any doc has a running stage
    for doc in batch.documents:
        stages = doc.pipeline_stages or {}
        if any(
            v.get("status") == StageStatus.RUNNING.value
            for v in stages.values()
            if isinstance(v, dict)
        ):
            return -1

    # Clear the cascade gate so the batch analysis can re-run
    batch.analysis_queued_at = None
    batch.status = IngestBatchStatus.PENDING
    if batch.meta:
        meta = dict(batch.meta)
        meta.pop("reload_fired", None)
        batch.meta = meta

    dispatch_items: list = []

    for doc in batch.documents:
        stages = doc.pipeline_stages or {}
        # Reset all non-EXTRACT stages to PENDING. We reset SKIPPED stages too
        # to ensure the cascade flows correctly (Fix for 'enrichment pending' bug).
        for stage in PipelineStage:
            if stage == PipelineStage.EXTRACT and not full:
                continue

            record = stages.get(stage.value, {})
            if record.get("status") == StageStatus.SKIPPED.value and record.get(
                "reason"
            ) in (
                "manual upload",
                "no batch (manual upload)",
            ):
                continue

            stages[stage.value] = {"status": StageStatus.PENDING.value}

        new_state = compute_overall_state(stages)
        doc.pipeline_stages = stages
        doc.pipeline_state = new_state

        # Reset analyzer-owned fields so the re-run converges instead of
        # layering on stale role/parent_id/originator data from the prior pass.
        # Manual case assignments (case_id) are preserved; proceeding_id/az_court
        # are owned by METADATA + PROCEEDING_ANALYSIS and self-heal on re-run.
        doc.role = DocumentRole.STANDALONE
        doc.parent_id = None
        doc.court_relay = False
        doc.attributed_originator = None

        # Full retry preservation logic: clear draft assignments so extraction
        # can re-run fresh, but keep manually confirmed ones.
        if full:
            # Case preservation
            if doc.case_id and doc.case_id != "_TRIAGE":
                c = db.query(Case).filter(Case.id == doc.case_id).first()
                if not c or c.is_draft:
                    doc.case_id = "_TRIAGE"
            # Proceeding preservation
            if doc.proceeding_id:
                p = (
                    db.query(Proceeding)
                    .filter(Proceeding.id == doc.proceeding_id)
                    .first()
                )
                if not p or p.is_draft:
                    doc.proceeding_id = None

        db.execute(
            _text(
                "UPDATE documents SET pipeline_stages = :stages, pipeline_state = :state, "
                "case_id = :case_id, proceeding_id = :proc_id "
                "WHERE id = :doc_id"
            ),
            {
                "stages": json.dumps(stages),
                "state": new_state.value,
                "case_id": doc.case_id,
                "proc_id": doc.proceeding_id,
                "doc_id": doc.id,
            },
        )

        # Build dispatch plan from the stages we just wrote.
        head: PipelineStage | None = None
        if full:
            head = PipelineStage.EXTRACT
        else:
            for spec in _STAGE_ORDER:
                if spec.stage in (PipelineStage.EXTRACT, PipelineStage.EMBEDDINGS):
                    continue
                status = stages.get(spec.stage.value, {}).get("status")
                if status not in (
                    StageStatus.COMPLETED.value,
                    StageStatus.SKIPPED.value,
                ):
                    head = spec.stage
                    break

        emb_status = stages.get(PipelineStage.EMBEDDINGS.value, {}).get("status")
        needs_emb = emb_status not in (
            StageStatus.COMPLETED.value,
            StageStatus.SKIPPED.value,
        )
        dispatch_items.append((doc.id, batch.id, head, needs_emb))

    batch_fallback = not any(head is not None for _, _, head, _ in dispatch_items)
    return dispatch_items, batch_fallback


def _retry_batch_dispatch(
    dispatch_items: list, *, batch_fallback: bool, batch_id: int, db
) -> None:
    """Dispatch Celery tasks from a plan built by _retry_batch_reset.

    Must only be called after the DB transaction for the reset has committed.
    Dispatch is fire-and-forget; errors are logged but not propagated.
    """
    from app.api.documents import _dispatch_retry_task
    from app.models.enums import PipelineStage

    for doc_id, b_id, head, needs_emb in dispatch_items:
        if head is not None:
            _dispatch_retry_task(doc_id, b_id, head)
        if needs_emb:
            _dispatch_retry_task(doc_id, b_id, PipelineStage.EMBEDDINGS)

    # Fallback: all per-doc cascade stages are already done but BATCH_ANALYSIS is still
    # pending — e.g. a batch-level retry after docs finished. The cascade won't fire it
    # naturally since no per-doc head task was dispatched.
    if batch_fallback:
        from app.services.intelligence.orchestrator import claim_batch_for_analysis

        if claim_batch_for_analysis(batch_id, db):
            from app.tasks.analyze_batch import analyze_batch_task

            analyze_batch_task.delay(batch_id)


@router.post("/triage/bundle/retry")
async def retry_bundle_pipeline(
    request: Request,
    batch_id: int = Form(...),
    full: str = Form("false"),
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Re-run every AI stage for every doc in the bundle.

    Skips EXTRACT (preserves manual case assignments) and leaves SKIPPED stages
    alone. Clears analysis_queued_at so the batch analysis can re-fire.
    Returns 409 if any stage is actively running.
    """
    from app.models.database import IngestBatch
    from app.services.pipeline_status import retry_on_db_locked

    is_full = full.lower() == "true"

    batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

    def _do_reset():
        result = _retry_batch_reset(batch, db, full=is_full)
        if result == -1:
            return -1, None, None
        items, batch_fallback = result
        db.commit()
        return items, batch_fallback, len(batch.documents)

    try:
        items, batch_fallback, doc_count = retry_on_db_locked(_do_reset, db)
    except OperationalError as exc:
        raise HTTPException(
            status_code=409,
            detail="Worker busy — try again in a moment",
        ) from exc

    if items == -1:
        raise HTTPException(
            status_code=409,
            detail="A pipeline stage is actively running — retry not allowed",
        )

    _retry_batch_dispatch(
        items, batch_fallback=batch_fallback, batch_id=batch_id, db=db
    )

    # OOB row update + global badges
    bundles = triage_service.get_triage_bundles()
    bundle_key = f"batch-{batch_id}"
    updated_bundle = next((b for b in bundles if b.key == bundle_key), None)

    oob_parts: list[str] = []
    if updated_bundle:
        oob_parts.append(
            render_bundle_group_oob(request, updated_bundle, triage_service)
        )
    oob_parts.append(render_sidebar_badges_oob(db))
    oob_parts.append(render_triage_header_stats_oob(request, triage_service))

    trigger = {"triage:bundle-retried": {"batch_id": batch_id, "doc_count": doc_count}}

    response = HTMLResponse(content="".join(oob_parts))
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


@router.post("/triage/retry-all")
async def retry_all_bundles(
    request: Request,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Retry the AI pipeline for every bundle currently in triage.

    Bundles with an actively running stage are skipped rather than rejected.
    Bundles that can't acquire the write lock after retries are also skipped.
    """
    from app.models.database import IngestBatch
    from app.services.pipeline_status import retry_on_db_locked
    from app.services.triage_view import render_triage_feed_oob

    bundles = triage_service.get_triage_bundles(limit=500)
    batch_ids = {b.batch_id for b in bundles if b.batch_id is not None}

    batches = db.query(IngestBatch).filter(IngestBatch.id.in_(batch_ids)).all()

    retried = 0
    for batch in batches:

        def _do_reset(b=batch):
            res = _retry_batch_reset(b, db, full=False)
            if res == -1:
                return -1
            its, bf = res
            db.commit()
            return its, bf

        try:
            result = retry_on_db_locked(_do_reset, db)
        except OperationalError:
            logger.warning(
                "retry-all: batch %s still locked after retries; skipping", batch.id
            )
            continue
        if result == -1:
            continue  # actively-running bundles are silently skipped
        items, batch_fallback = result
        _retry_batch_dispatch(
            items, batch_fallback=batch_fallback, batch_id=batch.id, db=db
        )
        retried += 1

    oob_parts = [
        render_triage_feed_oob(request, triage_service, db),
        render_sidebar_badges_oob(db),
        render_triage_header_stats_oob(request, triage_service),
    ]

    bundle_word = "bundle" if retried == 1 else "bundles"
    trigger = {
        "triage:bundles-retried": {
            "count": retried,
            "message": f"Retried {retried} {bundle_word}",
        }
    }

    response = HTMLResponse(content="".join(oob_parts))
    response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _render_document_hud(
    request: Request,
    doc: Document,
    db: Session,
    triage_service: TriageService,
) -> HTMLResponse:
    """Render the triage doc HUD — reused by reingest/summarize/approve-summary.

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
def triage_card_live(
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


# -----------------------------------------------------------------------------
# Bundle sub-group management (HTMX)
# -----------------------------------------------------------------------------


@router.post("/triage/bundle/{batch_id}/set-cover")
def triage_set_cover_letter(
    batch_id: int,
    doc_id: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Mark a document as cover letter of its sub-group."""
    triage_service.set_cover_letter(doc_id=doc_id, batch_id=batch_id)
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, triage_service))


@router.post("/triage/bundle/{batch_id}/new-group")
def triage_create_sub_group(
    batch_id: int,
    request: Request = None,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Create a new empty sub-group at the end of this batch's group list."""
    triage_service.create_sub_group(batch_id=batch_id)
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, triage_service))


@router.post("/triage/bundle/{batch_id}/rename-group")
def triage_rename_sub_group(
    batch_id: int,
    sub_group_id: str = Form(""),
    lead_doc_id: str = Form(""),
    label: str = Form(""),
    request: Request = None,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Rename a sub-group label. Empty label clears to auto-derived.

    sub_group_id may be empty when the bundle is still in auto mode; in that
    case lead_doc_id is used to identify the group after lazy init.
    """
    sub_group_id_int = int(sub_group_id) if sub_group_id.strip() else None
    lead_doc_id_int = int(lead_doc_id) if lead_doc_id.strip() else None
    triage_service.rename_sub_group(
        sub_group_id=sub_group_id_int,
        batch_id=batch_id,
        label=label,
        lead_doc_id=lead_doc_id_int,
    )
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, triage_service))


@router.post("/triage/bundle/{batch_id}/delete-group")
def triage_delete_sub_group(
    batch_id: int,
    sub_group_id: str = Form(""),
    lead_doc_id: str = Form(""),
    request: Request = None,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Delete a sub-group. Docs reassign to the next remaining group, or
    revert to auto mode if this was the only sub-group.

    sub_group_id may be empty when the bundle is still in auto mode; in that
    case lead_doc_id is used to identify the group after lazy init.
    """
    sub_group_id_int = int(sub_group_id) if sub_group_id.strip() else None
    lead_doc_id_int = int(lead_doc_id) if lead_doc_id.strip() else None
    triage_service.delete_sub_group(
        sub_group_id=sub_group_id_int,
        batch_id=batch_id,
        lead_doc_id=lead_doc_id_int,
    )
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, triage_service))


@router.post("/triage/bundle/{batch_id}/reorder")
def triage_reorder_documents(
    batch_id: int,
    sub_group_id: str = Form(""),
    lead_doc_id: str = Form(""),
    doc_ids: str = Form(...),
    request: Request = None,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Update document ordering and sub-group membership after drag-drop.

    Frontend sends one POST per affected sub-group with its full ordered doc list.
    doc_ids is a comma-separated string of integer doc ids.
    sub_group_id may be empty when the bundle is still in auto mode; in that
    case lead_doc_id is used to identify the group after lazy init.
    """
    sub_group_id_int = int(sub_group_id) if sub_group_id.strip() else None
    lead_doc_id_int = int(lead_doc_id) if lead_doc_id.strip() else None
    ordered_ids = [int(x) for x in doc_ids.split(",") if x.strip()]
    triage_service.reorder_documents(
        batch_id=batch_id,
        ordered_doc_ids=ordered_ids,
        target_sub_group_id=sub_group_id_int,
        lead_doc_id=lead_doc_id_int,
    )
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, triage_service))


@router.post("/triage/bundle/{batch_id}/reset-groups")
def triage_reset_sub_groups(
    batch_id: int,
    request: Request = None,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Remove manual sub-groups, reverting this batch to auto-grouped mode."""
    triage_service.reset_sub_groups(batch_id)
    db.commit()
    return HTMLResponse(content=_render_picker(request, batch_id, triage_service))


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


# -----------------------------------------------------------------------------
@router.get("/triage/bundle/{batch_id}")
def get_bundle(
    request: Request,
    batch_id: int,
    db: Session = Depends(get_db),
    triage_service: TriageService = Depends(get_triage_service),
):
    """Return the rendered HTML for a single bundle group (no OOB)."""
    bundle = triage_service.get_bundle_by_batch_id(batch_id)
    if not bundle:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

    reactions_by_doc = triage_service.get_reactions_by_doc_ids(
        [doc.id for doc in bundle.documents]
    )

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
def bundle_pipeline_status(
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

    # Bundle row re-renders on three cues so title, originator, and case
    # assignment become visible incrementally without a manual page refresh:
    #   metadata, batch_analysis, enrich each fire once when they first go
    #   terminal across the batch (latched in IngestBatch.meta["reload_fired"]).
    #   A final unconditional reload fires when every stage is terminal.
    fire_reload = n_total > 0 and n_done == n_total

    if not fire_reload and bool(stages_per_doc):
        _TERMINAL = {"completed", "failed", "skipped"}
        from app.models.database import IngestBatch

        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if batch is not None:
            meta = dict(batch.meta or {})
            fired = dict(meta.get("reload_fired") or {})

            for stage_key in ("metadata", "batch_analysis", "enrich"):
                if fired.get(stage_key):
                    continue
                if all(
                    (d.get(stage_key) or {}).get("status") in _TERMINAL
                    for d in stages_per_doc
                ):
                    fired[stage_key] = True
                    fire_reload = True

            if fire_reload:
                meta["reload_fired"] = fired
                batch.meta = meta
                # Celery workers write to ingest_batches concurrently, so
                # SQLite's single-writer lock can transiently lock us out.
                # The latch is idempotent — next poll retries — so brief
                # retry + skip-on-busy avoids 500s without losing correctness.
                for _attempt in range(3):
                    try:
                        db.commit()
                        break
                    except OperationalError as exc:
                        db.rollback()
                        if (
                            "database is locked" not in str(exc).lower()
                            or _attempt == 2
                        ):
                            logger.debug(
                                "pipeline latch commit busy, deferring: %s", exc
                            )
                            break
                        time.sleep(0.05 * (_attempt + 1))

    if fire_reload:
        response.headers["HX-Trigger"] = json.dumps(
            {f"reload-bundle-{batch_id}": {"ts": time.time()}}
        )

    return response


@router.post("/triage/batch/{batch_id}/retry-analysis")
def retry_batch_analysis(batch_id: int, db: Session = Depends(get_db)):
    """Manually retry batch analysis for a stuck or failed batch."""
    from app.models.database import IngestBatch
    from app.tasks.analyze_batch import analyze_batch_task

    batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    # Clear the claim to allow re-analysis
    batch.analysis_queued_at = None
    db.commit()

    # Trigger analysis
    analyze_batch_task.delay(batch_id)

    return {"status": "retry_scheduled", "batch_id": batch_id}
