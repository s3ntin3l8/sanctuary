"""Document review: the panel view and the actions it exposes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.access_guards import (
    check_owned_or_case_access,
    require_case_access,
    require_document_access,
    require_pin_access,
)
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import (
    Case,
    Document,
    DocumentRelationship,
    IngestBatch,
    User,
)
from app.models.enums import (
    PipelineStage,
    RelationshipConfidence,
    StageStatus,
)
from app.repositories.document_pin import DocumentPinRepository
from app.schemas.document_review import (
    CaseRef,
    CostPromoted,
    CostPromotion,
    DocumentReader,
    DocumentReview,
    DocumentStatus,
    MetadataUpdate,
    PinCreate,
    PinUpdate,
    PinView,
    PipelineView,
    ReactionUpdate,
    ReactionView,
    SummaryAction,
    SummaryBullet,
    SummaryView,
)
from app.services import api_token_service, pin_service, reaction_service
from app.services.document_review_service import (
    case_ref,
    pin_view,
    pipeline_view,
    reader_view,
    review_view,
)
from app.services.pipeline_status import (
    STAGE_REGISTRY,
    get_upstream_blocking,
    reset_all_stages,
    reset_stage,
    retry_on_db_locked,
    stages_dict,
)
from app.services.triage_retry import dispatch_pipeline_retry, rearm_batch_barriers

logger = logging.getLogger(__name__)
router = APIRouter(tags=["documents"])


# --- Review view and metadata ------------------------------------------------


@router.get("/documents/{doc_id}/review", response_model=DocumentReview)
def review(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access()),
):
    return review_view(db, user, doc)


@router.get("/documents/{doc_id}/reader", response_model=DocumentReader)
def reader(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access()),
):
    return reader_view(db, user, doc)


@router.get(
    "/documents/{doc_id}/original",
    response_class=FileResponse,
    responses={200: {"content": {"application/pdf": {}}}},
)
def original(doc: Document = Depends(require_document_access())):
    """The stored source file, inline for PDFs and as a download otherwise."""
    from app.config import DATA_DIR
    from app.core.paths import resolve_storage_path

    if not doc.file_path:
        raise ApiError(404, "no_original", "No original file stored for this document.")
    resolved = resolve_storage_path(doc.file_path).resolve()
    data_root = DATA_DIR.resolve()
    # Refuse anything outside DATA_DIR even if file_path were ever attacker-influenced.
    if not str(resolved).startswith(str(data_root) + "/") or not resolved.exists():
        raise ApiError(404, "no_original", "Original file not found on disk.")
    if resolved.suffix.lower() == ".pdf":
        return FileResponse(
            path=str(resolved),
            filename=resolved.name,
            media_type="application/pdf",
            content_disposition_type="inline",
        )
    return FileResponse(
        path=str(resolved),
        filename=resolved.name,
        media_type="application/octet-stream",
    )


@router.post("/documents/{doc_id}/pins", response_model=PinView, status_code=201)
@limiter.limit("60/minute")
def create_pin(
    request: Request,
    body: PinCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Anchor a margin note to one of the document's key passages."""
    try:
        pin = pin_service.create_pin(db, user.id, doc, body.passage_id, body.note)
    except pin_service.UnknownPassage as exc:
        raise ApiError(
            422, "unknown_passage", "No such passage in this document."
        ) from exc
    db.commit()
    db.refresh(pin)
    return pin_view(pin, api_token_service.token_names(db, [pin.agent_token_id]))


@router.patch("/pins/{pin_id}", response_model=PinView)
def update_pin(
    body: PinUpdate,
    db: Session = Depends(get_db),
    pin=Depends(require_pin_access(edit=True)),
):
    DocumentPinRepository(db).update_note(pin.id, body.note)
    db.commit()
    db.refresh(pin)
    return pin_view(pin, api_token_service.token_names(db, [pin.agent_token_id]))


@router.delete("/pins/{pin_id}", status_code=204, response_class=Response)
def delete_pin(
    db: Session = Depends(get_db),
    pin=Depends(require_pin_access(edit=True)),
):
    DocumentPinRepository(db).delete(pin.id)
    db.commit()


@router.put("/documents/{doc_id}/metadata", response_model=DocumentReview)
@limiter.limit("30/minute")
def update_metadata(
    request: Request,
    body: MetadataUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Apply a metadata patch (fields left null are untouched) and recompute review flags."""
    from app.services.triage_confirmation import confirm_document

    updated = confirm_document(
        db,
        doc.id,
        title=(body.title.strip() or None) if body.title else None,
        originator_type=body.originator_type,
        sender=body.sender.strip() if body.sender is not None else None,
        internal_id=body.internal_id.strip() if body.internal_id is not None else None,
        az_court=body.az_court,
        issued_date=body.issued_date,
        received_date=body.received_date,
        significance_tier=body.significance_tier,
        document_type=body.document_type,
        finalize=False,
    )
    if updated is None:
        raise ApiError(404, "not_found", "Document not found.")
    db.refresh(updated)
    return review_view(db, user, updated)


@router.post(
    "/documents/{doc_id}/ocr-unverified/acknowledge", response_model=DocumentReview
)
def acknowledge_ocr_unverified(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Mark the pages the OCR cross-check could not corroborate as checked against
    the scan. A re-extract runs the cross-check afresh."""
    from app.services.ingestion.service import refresh_review_reasons

    meta = dict(doc.meta or {})
    if "ocr_unverified_pages" in meta:
        meta["ocr_unverified_acknowledged"] = meta.pop("ocr_unverified_pages")
        doc.meta = meta
        refresh_review_reasons(doc, db, commit=False)
        db.commit()
    db.refresh(doc)
    return review_view(db, user, doc)


@router.post(
    "/documents/{doc_id}/contradiction/acknowledge", response_model=DocumentReview
)
def acknowledge_contradiction(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Dismiss the AI's contradiction flag. A re-enrich that reports the same
    notes leaves it dismissed; different notes raise it again."""
    from app.services.ingestion.service import refresh_review_reasons

    meta = dict(doc.meta or {})
    meta["ai_contradiction"] = False
    meta["contradiction_acknowledged"] = meta.get("contradiction_notes") or []
    doc.meta = meta
    refresh_review_reasons(doc, db, commit=False)
    db.commit()
    db.refresh(doc)
    return review_view(db, user, doc)


@router.post("/documents/{doc_id}/summary", response_model=SummaryView)
def summary_action(
    body: SummaryAction,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    from app.services.case_dashboard_service import summary_bullets_from_ai_summary

    if body.action == "approve":
        if not summary_bullets_from_ai_summary(doc.ai_summary):
            raise ApiError(409, "no_summary", "There is no summary to approve.")
        doc.ai_summary_approved_at = datetime.now(UTC)
    elif body.action == "unapprove":
        doc.ai_summary_approved_at = None
    else:
        doc.ai_summary = None
        doc.ai_summary_approved_at = None
    db.commit()
    db.refresh(doc)
    enrich = (stages_dict(doc).get("enrich") or {}).get("status")
    return SummaryView(
        bullets=[
            SummaryBullet(**b)
            for b in summary_bullets_from_ai_summary(doc.ai_summary)
            if b.get("kind") in ("legal", "action", "finance")
        ],
        approved_at=doc.ai_summary_approved_at,
        created_at=doc.ai_summary_created_at,
        enrich_status=StageStatus(enrich) if enrich else None,
    )


@router.post("/documents/{doc_id}/reactions", response_model=list[ReactionView])
def set_reaction(
    body: ReactionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Toggle a reaction; with ``notes`` it is upserted instead of toggled."""
    reactions = reaction_service.set_reaction(
        db, user.id, doc, body.reaction, body.notes
    )
    db.commit()
    names = api_token_service.token_names(db, (r.agent_token_id for r in reactions))
    return [
        ReactionView(
            reaction=r.reaction,
            notes=r.notes,
            created_at=r.ingest_date,
            agent_token_name=names.get(r.agent_token_id),
        )
        for r in reactions
    ]


@router.delete("/documents/{doc_id}", status_code=204, response_class=Response)
def delete_document(
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    from app.services.document_service import DocumentService
    from app.services.triage_confirmation import cleanup_orphaned_drafts

    DocumentService(db).delete_document(doc.id)
    cleanup_orphaned_drafts(db)


@router.get("/documents/{doc_id}/status", response_model=DocumentStatus)
def document_status(doc: Document = Depends(require_document_access())):
    """Upload-progress polling: terminal states carry no further changes."""
    stages = stages_dict(doc)
    state = doc.pipeline_state
    error = None
    label = "queued"
    if state.value == "failed":
        for key, rec in stages.items():
            if isinstance(rec, dict) and rec.get("status") == "failed":
                label = f"{key.replace('_', ' ')} failed"
                error = rec.get("error")
                break
        else:
            label = "failed"
    elif state.value == "completed":
        label = "ready"
    else:
        for key, rec in stages.items():
            if isinstance(rec, dict) and rec.get("status") == "retrying":
                label = f"retrying {key.replace('_', ' ')}"
                break
        else:
            for key, rec in stages.items():
                if isinstance(rec, dict) and rec.get("status") == "running":
                    label = f"{key.replace('_', ' ')}…"
                    break
    return DocumentStatus(id=doc.id, state=state, label=label, error=error)


@router.post("/documents/{doc_id}/cost-signals/promote", response_model=CostPromoted)
def promote_cost_signal(
    body: CostPromotion,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Create a LegalCost from the document's most recent cost signal.

    Invoice/advance signals already materialise during enrichment; this is
    for the remaining kinds (e.g. a Streitwert ruling) with optional overrides.
    """
    from app.models.database import CostSignal, LegalCost
    from app.models.enums import CostCategory
    from app.services.case_service import recompute_total_cost_exposure

    if not doc.case_id or doc.case_id == "_TRIAGE":
        raise ApiError(422, "no_case", "Document has no case.")
    signal = (
        db.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .order_by(
            CostSignal.issued_at.desc().nullslast(), CostSignal.ingest_date.desc()
        )
        .first()
    )
    if signal is None and body.amount is None:
        raise ApiError(
            422, "no_signal", "No cost signal on this document and no amount given."
        )
    amount = (
        float(body.amount)
        if body.amount is not None
        else float((signal.amount if signal else 0) or 0)
    )
    category = CostCategory.SONSTIGES
    if body.category:
        try:
            category = CostCategory(body.category)
        except ValueError as exc:
            raise ApiError(
                422, "invalid_category", f"Unknown category: {body.category}"
            ) from exc
    vat_rate = body.vat_rate if body.vat_rate is not None else 0.0
    cost = LegalCost(
        case_id=doc.case_id,
        proceeding_id=doc.proceeding_id,
        category=category,
        title=(signal.description if signal else None)
        or doc.title
        or "Cost from document",
        amount_net=amount,
        vat_rate=vat_rate,
        amount_gross=amount * (1 + vat_rate),
        source_document_id=doc.id,
        issued_at=doc.issued_date or doc.ingest_date,
    )
    db.add(cost)
    db.commit()
    db.refresh(cost)
    recompute_total_cost_exposure(doc.case_id, db)
    return CostPromoted(cost_id=cost.id, amount_gross=cost.amount_gross)


# --- Action items, relationships ----------------------------------------------


def _owned_relationship(
    rel_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DocumentRelationship:
    """404 unless the user may edit *both* documents of the relationship."""
    rel = db.get(DocumentRelationship, rel_id)
    if rel is None:
        raise ApiError(404, "not_found", "Relationship not found.")
    for doc_id in (rel.from_document_id, rel.to_document_id):
        doc = db.get(Document, doc_id)
        if doc is None:
            raise ApiError(404, "not_found", "Relationship not found.")
        allowed = check_owned_or_case_access(
            db, user, owner_id=doc.owner_id, case_id=doc.case_id, edit=True
        )
        if not allowed:
            raise ApiError(404, "not_found", "Relationship not found.")
    return rel


@router.post(
    "/relationships/{rel_id}/confirm", status_code=204, response_class=Response
)
def confirm_relationship(
    rel: DocumentRelationship = Depends(_owned_relationship),
    db: Session = Depends(get_db),
):
    """Promote an AI-detected relationship to user-confirmed; closes the target's thread.

    Email-header edges are already facts about the mail — there is nothing to
    confirm, and promoting one would turn a References-only link into a
    thread-closing claim. They can still be rejected.
    """
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    if rel.confidence == RelationshipConfidence.EMAIL_HEADER:
        return
    source_id, target_id = rel.from_document_id, rel.to_document_id
    rel.confidence = RelationshipConfidence.USER_CONFIRMED
    db.commit()
    recompute_thread_open(target_id, db)
    source = db.get(Document, source_id)
    if source:
        refresh_review_reasons(source, db)


@router.delete("/relationships/{rel_id}", status_code=204, response_class=Response)
def reject_relationship(
    rel: DocumentRelationship = Depends(_owned_relationship),
    db: Session = Depends(get_db),
):
    """Drop a relationship and remember the rejection so later detection runs
    don't recreate it; reopens the target's thread if nothing confirmed remains."""
    from app.repositories.document_relationship import reject_edge
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    source_id, target_id = rel.from_document_id, rel.to_document_id
    reject_edge(db, rel)
    db.commit()
    recompute_thread_open(target_id, db)
    source = db.get(Document, source_id)
    if source:
        refresh_review_reasons(source, db)


# --- Pipeline retries --------------------------------------------------------


def _lock_row(doc_id: int, db: Session) -> None:
    from sqlalchemy import text

    db.execute(
        text("SELECT id FROM documents WHERE id = :doc_id FOR UPDATE"),
        {"doc_id": doc_id},
    )


@router.post("/documents/{doc_id}/pipeline/{stage}/retry", response_model=PipelineView)
@limiter.limit("30/minute")
def retry_stage(
    request: Request,
    stage: PipelineStage,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    _lock_row(doc.id, db)
    db.refresh(doc)
    stages = stages_dict(doc)
    current = (stages.get(stage.value) or {}).get("status")
    if current in ("running", "retrying"):
        raise ApiError(409, "in_flight", f"Stage '{stage.value}' is already {current}.")
    blocking = get_upstream_blocking(stage, stages)
    if blocking:
        labels = ", ".join(STAGE_REGISTRY[PipelineStage(b)].label for b in blocking)
        raise ApiError(
            409,
            "upstream_running",
            f"Waiting for {labels} — {STAGE_REGISTRY[stage].label} re-runs automatically afterwards.",
        )
    if stage == PipelineStage.BATCH_ANALYSIS and doc.ingest_batch_id:
        siblings = (
            db.query(Document)
            .filter(Document.ingest_batch_id == doc.ingest_batch_id)
            .all()
        )
        reset_any = False
        claimed_since_read: list[int] = []
        for sibling in siblings:
            if (stages_dict(sibling).get("batch_analysis") or {}).get(
                "status"
            ) == "failed":
                if reset_stage(sibling.id, PipelineStage.BATCH_ANALYSIS, db):
                    reset_any = True
                else:
                    claimed_since_read.append(sibling.id)
        if claimed_since_read and not reset_any:
            # Every failed sibling was re-claimed by another dispatcher after the
            # read above: that retry is already running, don't start a second.
            raise ApiError(
                409,
                "in_flight",
                "Batch analysis is already being retried "
                f"(documents {', '.join(map(str, claimed_since_read))}).",
            )
    elif not reset_stage(doc.id, stage, db):
        # A dispatcher claimed the stage after the status check above.
        raise ApiError(409, "in_flight", f"Stage '{stage.value}' is already running.")
    if stage == PipelineStage.EXTRACT and doc.ingest_batch_id:
        rearm_batch_barriers(doc.ingest_batch_id, db)
    db.refresh(doc)
    dispatch_pipeline_retry(doc.id, doc.ingest_batch_id, stage, db)
    return pipeline_view(doc)


@router.post("/documents/{doc_id}/pipeline/retry-all", response_model=PipelineView)
@limiter.limit("30/minute")
def retry_all_stages(
    request: Request,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    def _do_reset():
        _lock_row(doc.id, db)
        db.refresh(doc)
        # All-or-nothing and atomic with the in-flight check: returns the
        # running stage keys (nothing reset) or [] after resetting everything.
        return reset_all_stages(doc.id, db)

    try:
        running = retry_on_db_locked(_do_reset, db)
    except OperationalError as exc:
        raise ApiError(
            409, "worker_busy", "Worker busy — try again in a moment."
        ) from exc
    if running:
        raise ApiError(
            409, "in_flight", "Stage(s) still running: " + ", ".join(running)
        )
    if doc.ingest_batch_id:
        batch = db.get(IngestBatch, doc.ingest_batch_id)
        if batch is not None and batch.meta:
            meta = dict(batch.meta)
            meta.pop("reload_fired", None)
            batch.meta = meta
            db.commit()
        # Commits on its own; must run before the EXTRACT dispatch below.
        rearm_batch_barriers(doc.ingest_batch_id, db)
    db.refresh(doc)
    dispatch_pipeline_retry(doc.id, doc.ingest_batch_id, PipelineStage.EXTRACT, db)
    return pipeline_view(doc)


# --- Draft cases ---------------------------------------------------------------


@router.post("/cases/{case_id}/confirm-draft", response_model=CaseRef)
def confirm_draft(
    db: Session = Depends(get_db), case: Case = Depends(require_case_access(edit=True))
):
    """Ratify an AI-created draft case."""
    if case.is_draft:
        case.is_draft = False
        db.commit()
    return case_ref(case)


@router.post("/cases/{case_id}/reject-draft", status_code=204, response_class=Response)
def reject_draft(
    case_id: str,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Delete an AI-created draft case; its documents return to triage."""
    from app.services.case_service import CaseService

    if not case.is_draft:
        raise ApiError(400, "not_draft", "Only draft cases can be rejected.")
    try:
        result = CaseService(db).delete_and_revert(case_id)
    except ValueError as exc:
        raise ApiError(400, "invalid", str(exc)) from exc
    if result is None:
        raise ApiError(404, "not_found", "Case not found.")
