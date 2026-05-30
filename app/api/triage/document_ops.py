"""Per-document mutations: confirm, bundle confirm, batch confirm/assign, title patch."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.models.database import Case, Document, IngestBatch
from app.models.enums import OriginatorType
from app.repositories.case import CaseRepository
from app.services.hud_context import build_hud_context
from app.services.pipeline_status import stages_dict
from app.services.triage_bundles import get_triage_bundles
from app.services.triage_confirmation import (
    confirm_bundle as _confirm_bundle,
)
from app.services.triage_confirmation import (
    confirm_document as _confirm_document,
)
from app.services.triage_confirmation import (
    find_next_review_doc,
    get_bundle_suggestion,
)
from app.services.triage_oob_render import (
    render_batch_oob,
    render_bundle_group_oob,
    render_row_targeted_oob,
    render_sidebar_badges_oob,
    render_triage_header_stats_oob,
)

router = APIRouter()


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


def _key_owned_by(
    db: Session, batch_id: int | None, doc_id: int | None, user_id: int
) -> bool:
    """Whether the bundle key's batch/doc belongs to the user (per-user triage)."""
    if batch_id is not None:
        row = db.query(IngestBatch.owner_id).filter(IngestBatch.id == batch_id).first()
        return row is not None and row[0] == user_id
    if doc_id is not None:
        row = db.query(Document.owner_id).filter(Document.id == doc_id).first()
        return row is not None and row[0] == user_id
    return False


def _require_editable_target(
    db: Session, case_id: str | None, request: Request
) -> None:
    """403 when assigning a triage item to an EXISTING case the user can't edit.

    Prevents a cross-tenant write: owning your triage bundle does not let you
    file it into someone else's case. New cases (just created here, owned by the
    user) and the _TRIAGE pseudo-case are always allowed.
    """
    if not case_id or case_id == "_TRIAGE":
        return
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        return  # nonexistent target — let the downstream FK handling deal with it
    from app.services import access_service

    if not access_service.can_edit_case(db, request.state.current_user, case):
        raise HTTPException(status_code=403, detail="You cannot assign to that case")


@router.post("/triage/document/{doc_id}/confirm")
@limiter.limit("30/minute")
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
):
    from app.models.enums import DocumentType, SignificanceTier

    resolved_case_id = case_id if case_id else None
    _require_editable_target(db, resolved_case_id, request)

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

    doc = _confirm_document(
        db,
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
        from app.services.triage_confirmation import reset_and_reenrich

        reset_and_reenrich(db, [doc])

    # Offer to re-run pipeline from enrich when significance tier was changed and
    # enrichment has already completed (no point prompting if pipeline is still running).
    tier_requeue_prompt = (
        parsed_significance is not None
        and pre_confirm_tier != parsed_significance
        and stages_dict(doc).get("enrich", {}).get("status") in ("completed", "skipped")
    )

    cases = CaseRepository(db).list_for_picker()
    ctx = build_hud_context(db, doc, mode="review", context="embedded", cases=cases)
    ctx["tier_requeue_prompt"] = tier_requeue_prompt
    response = templates.TemplateResponse(request, "partials/triage/_doc_hud.html", ctx)
    # Targeted OOB: update only the affected card + bundle footer + badge.
    targeted_oob = render_row_targeted_oob(request, doc, db)
    # Global OOB: sidebar badges + status bar
    global_oob = render_sidebar_badges_oob(db, owner_id=request.state.current_user.id)
    global_oob += render_triage_header_stats_oob(
        request, db, owner_id=request.state.current_user.id
    )

    response.body += (targeted_oob + global_oob).encode("utf-8")

    # Confirm-and-next: if the doc is now out of triage, tell the client which
    # doc to advance to. Alpine listener picks this up from the HX-Trigger
    # header and shifts focus.
    if not doc.needs_review and doc.case_id and doc.case_id != "_TRIAGE":
        trigger: dict = {}
        next_doc = find_next_review_doc(db, doc.id)
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


@router.post("/triage/confirm")
@limiter.limit("30/minute")
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
            owner_id=request.state.current_user.id,
        )
        db.flush()
        case_id = new_case_obj.id

    if not case_id:
        raise HTTPException(status_code=422, detail="case_id is required")

    _require_editable_target(db, case_id, request)

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
    from app.services.triage_confirmation import reset_and_reenrich

    if is_synthetic == "true" and doc_id:
        _doc_id = int(doc_id)
        bundle_key = f"loose-{_doc_id}"
        pre_case = db.query(Document.case_id).filter(Document.id == _doc_id).scalar()
        updated_doc = _confirm_document(db, _doc_id, case_id=case_id, finalize=finalize)
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
            reset_and_reenrich(db, [updated_doc])
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
        batch = _confirm_bundle(
            db,
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
            reset_and_reenrich(db, pre_triage_docs)

    # ---- build targeted OOB response (no full feed replacement) ----
    bundles = get_triage_bundles(db, owner_id=request.state.current_user.id)
    updated_bundle = next((b for b in bundles if b.key == bundle_key), None)

    oob_parts: list[str] = []
    trigger: dict = {"triage:bundle-confirmed": {}}

    if updated_bundle:
        # Bundle still in triage — OOB-swap the whole bundle group (updates
        # case chip in header, all cards, footer, badge in one shot).
        oob_parts.append(render_bundle_group_oob(request, updated_bundle, db))
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
    oob_parts.append(
        render_sidebar_badges_oob(db, owner_id=request.state.current_user.id)
    )
    oob_parts.append(
        render_triage_header_stats_oob(
            request, db, owner_id=request.state.current_user.id
        )
    )

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


@router.post("/triage/batch/confirm")
@limiter.limit("30/minute")
async def batch_confirm(
    request: Request,
    bundle_keys: list[str] = Form(...),
    db: Session = Depends(get_db),
):
    """Silently confirm each selected bundle to its own AI-suggested case+proceeding.

    Bundles without a suggestion are skipped. Returns OOB swaps for all affected
    rows and fires `triage:batch-confirmed` with confirmed/skipped counts.
    """
    from app.services.triage_confirmation import reset_and_reenrich

    confirmed_count = 0
    skipped_count = 0
    confirmed_keys: list[str] = []

    for key in bundle_keys:
        batch_id, doc_id = _parse_bundle_key(key)
        if batch_id is None and doc_id is None:
            skipped_count += 1
            continue
        if not _key_owned_by(db, batch_id, doc_id, request.state.current_user.id):
            skipped_count += 1
            continue

        case_id, proceeding_id = get_bundle_suggestion(
            db, batch_id=batch_id, doc_id=doc_id
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
            batch = _confirm_bundle(
                db,
                batch_id,
                case_id=case_id,
                proceeding_id=proceeding_id,
                finalize=True,
            )
            if batch and case_id != "_TRIAGE" and pre_triage_docs:
                for d in pre_triage_docs:
                    db.refresh(d)
                reset_and_reenrich(db, pre_triage_docs)
        else:
            pre_case = db.query(Document.case_id).filter(Document.id == doc_id).scalar()
            updated_doc = _confirm_document(db, doc_id, case_id=case_id, finalize=True)
            if updated_doc and proceeding_id:
                updated_doc.proceeding_id = proceeding_id
                db.commit()
                db.refresh(updated_doc)
            if (
                updated_doc
                and (not pre_case or pre_case == "_TRIAGE")
                and case_id != "_TRIAGE"
            ):
                reset_and_reenrich(db, [updated_doc])

        confirmed_keys.append(key)
        confirmed_count += 1

    oob_html = render_batch_oob(
        request, bundle_keys, db, owner_id=request.state.current_user.id
    )
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
@limiter.limit("30/minute")
async def batch_assign(
    request: Request,
    bundle_keys: list[str] = Form(...),
    case_id: str | None = Form(None),
    new_case_id: str | None = Form(None),
    new_case_title: str | None = Form(None),
    proceeding_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Assign all selected bundles to a single case and optional proceeding.

    Mirrors the logic of POST /triage/confirm but applied to a list of bundle
    keys at once. Returns OOB swaps for all affected rows.
    """
    from app.services.case_service import get_or_create_case_from_reference
    from app.services.triage_confirmation import reset_and_reenrich

    if new_case_id:
        new_case_obj, _, _ = get_or_create_case_from_reference(
            db,
            internal_id=new_case_id,
            batch_subject=new_case_title or None,
            is_draft=False,
            owner_id=request.state.current_user.id,
        )
        db.flush()
        case_id = new_case_obj.id

    if not case_id:
        raise HTTPException(status_code=422, detail="case_id is required")

    _require_editable_target(db, case_id, request)

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
        if not _key_owned_by(db, batch_id, doc_id, request.state.current_user.id):
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
            batch = _confirm_bundle(
                db,
                batch_id,
                case_id=case_id,
                proceeding_id=parsed_proceeding_id,
                finalize=False,
            )
            if batch and case_id != "_TRIAGE" and pre_triage_docs:
                for d in pre_triage_docs:
                    db.refresh(d)
                reset_and_reenrich(db, pre_triage_docs)
        else:
            pre_case = db.query(Document.case_id).filter(Document.id == doc_id).scalar()
            updated_doc = _confirm_document(db, doc_id, case_id=case_id, finalize=False)
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
                reset_and_reenrich(db, [updated_doc])

        assigned_count += 1

    case_obj = db.query(Case).filter(Case.id == case_id).first()
    oob_html = render_batch_oob(
        request, bundle_keys, db, owner_id=request.state.current_user.id
    )
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
