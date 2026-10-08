"""Document review: the panel view and the actions it exposes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.access_guards import (
    check_owned_or_case_access,
    require_action_item_access,
    require_case_access,
    require_document_access,
    require_pin_access,
)
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import (
    ActionItem,
    Case,
    Document,
    DocumentRelationship,
    IngestBatch,
    Proceeding,
    User,
)
from app.models.enums import (
    PipelineStage,
    RelationshipConfidence,
    StageStatus,
)
from app.repositories.case import CaseRepository
from app.repositories.document_pin import DocumentPinRepository
from app.repositories.user_reaction import UserReactionRepository
from app.schemas.document_review import (
    ActionStatusUpdate,
    ActionView,
    CaseRef,
    CostPromoted,
    CostPromotion,
    CostSignalView,
    DocumentReader,
    DocumentReview,
    DocumentStatus,
    GroundView,
    KeyPassage,
    MetadataField,
    MetadataUpdate,
    PinCreate,
    PinUpdate,
    PinView,
    PipelineView,
    ProceedingRef,
    ReactionUpdate,
    ReactionView,
    ReaderNav,
    RelationshipView,
    StageView,
    SummaryAction,
    SummaryBullet,
    SummaryView,
)
from app.services import access_service
from app.services.case_dashboard_service import key_passages_for_template
from app.services.hud_context import build_hud_context
from app.services.markdown_render import render_highlighted
from app.services.pipeline_status import (
    STAGE_REGISTRY,
    get_upstream_blocking,
    reset_all_stages,
    reset_stage,
    retry_on_db_locked,
    stages_dict,
)
from app.services.triage_retry import dispatch_pipeline_retry

router = APIRouter(tags=["documents"])

METADATA_FIELDS = (
    ("originator_type", "Originator"),
    ("document_type", "Type"),
    ("sender", "Sender"),
    ("internal_id", "Reference"),
    ("issued_date", "Issued"),
    ("received_date", "Received"),
    ("significance_tier", "Tier"),
    ("az_court", "AZ"),
)


def _case_ref(case: Case) -> CaseRef:
    return CaseRef(id=case.id, title=case.title, is_draft=case.is_draft)


def _proceeding_ref(p: Proceeding) -> ProceedingRef:
    return ProceedingRef(
        id=p.id,
        case_id=p.case_id,
        court_name=p.court_name,
        az_court=p.az_court,
        court_level=p.court_level.value if p.court_level else "other",
        is_draft=bool(p.is_draft),
    )


def pipeline_view(doc: Document) -> PipelineView:
    stages = stages_dict(doc)
    out = []
    for spec in sorted(STAGE_REGISTRY.values(), key=lambda s: s.order):
        rec = stages.get(spec.stage.value) or {}
        status = rec.get("status")
        out.append(
            StageView(
                key=spec.stage,
                label=spec.label,
                icon=spec.icon,
                status=StageStatus(status) if status else None,
                error=rec.get("error"),
                attempt=rec.get("attempt"),
                max_attempts=rec.get("max_attempts"),
                next_at=rec.get("next_at"),
                completed_at=rec.get("completed_at"),
            )
        )
    failures = (doc.meta or {}).get("page_failures") or []
    return PipelineView(
        state=doc.pipeline_state,
        stages=out,
        ocr_page_failures=sorted(int(p) for p in failures),
    )


def _field_value(doc: Document, field: str) -> str | None:
    value = getattr(doc, field)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return getattr(value, "value", value)


def review_view(db: Session, user: User, doc: Document) -> DocumentReview:
    return DocumentReview(**_review_fields(db, user, doc)[0])


def _pin_view(pin) -> PinView:
    return PinView(
        id=pin.id,
        passage_id=pin.passage_id,
        note=pin.note,
        user_id=pin.user_id,
        updated_at=pin.updated_at,
    )


def reader_view(db: Session, user: User, doc: Document) -> DocumentReader:
    """The review view plus everything only the full-screen HUD shows."""
    fields, ctx = _review_fields(db, user, doc)
    body_html = (
        str(
            render_highlighted(
                doc.content,
                ctx["key_passages"],
                ctx["passage_claim_map"],
                ctx["claim_excerpt_map"],
            )
        )
        if doc.content
        else None
    )
    return DocumentReader(
        **fields,
        body_html=body_html,
        pins=[_pin_view(p) for p in ctx["pins"]],
        nav=ReaderNav(
            prev_doc_id=ctx["prev_doc_id"],
            next_doc_id=ctx["next_doc_id"],
            position=ctx["doc_position"],
            total=ctx["proceeding_total"],
            parent_id=doc.parent_id,
            first_child_id=ctx["first_child_id"],
            bundle_prev_id=ctx["bundle_prev_id"],
            bundle_next_id=ctx["bundle_next_id"],
        ),
        thread_open=bool(doc.thread_open),
        context_strategy=(doc.meta or {}).get("ai_context_strategy"),
        has_original=bool(doc.file_path),
    )


def _review_fields(db: Session, user: User, doc: Document) -> tuple[dict, dict]:
    cases = list(CaseRepository(db).list_for_picker(owner_id=user.id))
    ctx = build_hud_context(
        db, doc, viewer=user, mode="review", context="embedded", cases=cases
    )
    conf = doc.extraction_confidence or {}
    editable = access_service.editable_case_ids(db, user)
    proc_q = db.query(Proceeding).order_by(Proceeding.court_name.asc())
    if editable is not None:
        proc_q = proc_q.filter(Proceeding.case_id.in_(editable))
    enrich = (stages_dict(doc).get("enrich") or {}).get("status")
    fields: dict = {
        "id": doc.id,
        "title": doc.title,
        "original_filename": doc.original_filename,
        "page_count": doc.page_count or 0,
        "case_id": doc.case_id,
        "case": _case_ref(ctx["current_case"]) if ctx.get("current_case") else None,
        "proceeding": _proceeding_ref(doc.proceeding) if doc.proceeding else None,
        "originator_type": doc.originator_type,
        "attributed_originator": doc.attributed_originator,
        "court_relay": bool(doc.court_relay),
        "sender": doc.sender,
        "internal_id": doc.internal_id,
        "az_court": doc.az_court,
        "issued_date": doc.issued_date,
        "received_date": doc.received_date,
        "ingest_date": doc.ingest_date,
        "document_type": doc.document_type,
        "significance_tier": doc.significance_tier,
        "needs_review": bool(doc.needs_review),
        "review_reasons": list(doc.review_reasons or []),
        "content_hash": doc.content_hash,
        "metadata": [
            MetadataField(
                field=field,
                label=label,
                value=_field_value(doc, field),
                confidence=conf.get(field)
                if conf.get(field) in ("high", "medium", "low")
                else None,
            )
            for field, label in METADATA_FIELDS
        ],
        "pipeline": pipeline_view(doc),
        "summary": SummaryView(
            bullets=[
                SummaryBullet(**b)
                for b in ctx["summary_bullets"]
                if b.get("kind") in ("legal", "action", "finance")
            ],
            approved_at=doc.ai_summary_approved_at,
            created_at=doc.ai_summary_created_at,
            enrich_status=StageStatus(enrich) if enrich else None,
        ),
        "key_passages": [
            KeyPassage(
                id=p["id"],
                text=p["text"],
                kind=p.get("kind"),
                page=p.get("page"),
                rationale=p.get("rationale"),
                start_offset=p.get("start_offset"),
                end_offset=p.get("end_offset"),
                claim_id=ctx["passage_claim_map"].get(p["id"]),
                pin_count=ctx["passage_pin_counts"].get(p["id"], 0),
            )
            for p in ctx["key_passages"]
        ],
        "relationships": [
            RelationshipView(
                id=r["rel_obj"].id,
                doc_id=r["id"],
                title=r["title"],
                rel_type=r["rel_obj"].relationship_type,
                confidence=r["rel_obj"].confidence
                or RelationshipConfidence.AI_DETECTED,
                direction=side,  # type: ignore[arg-type]
            )
            for side, rels in (
                ("out", ctx["relationships_out"]),
                ("in", ctx["relationships_in"]),
            )
            for r in rels
        ],
        "grounds": [
            GroundView(
                id=c.id,
                claim_text=c.claim_text,
                claim_type=c.claim_type,
                status=c.status,
                is_precedent=c.is_precedent,
                first_made_at=c.first_made_at,
            )
            for c in ctx["grounds"]
        ],
        "claims_status": ctx["claims_status"],
        "actions": [
            ActionView(
                id=a.id,
                title=a.title,
                description=a.description,
                due_date=a.due_date,
                action_type=a.action_type,
                status=a.status,
                location=a.location,
                addressee=a.addressee,
            )
            for a in ctx["actions"]
        ],
        "cost_signals": [
            CostSignalView(
                id=s.id,
                signal_type=s.signal_type,
                amount=s.amount,
                description=s.description,
                issued_at=s.issued_at,
            )
            for s in doc.cost_signals
        ],
        "reactions": [
            ReactionView(reaction=r.reaction, notes=r.notes, created_at=r.ingest_date)
            for r in ctx["reactions"]
        ],
        "bundle_prev_id": ctx["bundle_prev_id"],
        "bundle_next_id": ctx["bundle_next_id"],
        "cases": [_case_ref(c) for c in ctx["cases"]],
        "proceedings": [_proceeding_ref(p) for p in proc_q.all()],
    }
    return fields, ctx


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
    known = {p["id"] for p in key_passages_for_template(doc.key_passages or [])}
    if body.passage_id not in known:
        raise ApiError(422, "unknown_passage", "No such passage in this document.")
    pin = DocumentPinRepository(db).create(
        doc.id, body.passage_id, body.note, user_id=user.id
    )
    db.commit()
    db.refresh(pin)
    return _pin_view(pin)


@router.patch("/pins/{pin_id}", response_model=PinView)
def update_pin(
    body: PinUpdate,
    db: Session = Depends(get_db),
    pin=Depends(require_pin_access(edit=True)),
):
    DocumentPinRepository(db).update_note(pin.id, body.note)
    db.commit()
    db.refresh(pin)
    return _pin_view(pin)


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


@router.post("/documents/{doc_id}/summary", response_model=SummaryView)
def summary_action(
    body: SummaryAction,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    from app.services.case_dashboard_service import summary_bullets_from_ai_summary

    if body.action == "approve":
        doc.ai_summary_approved_at = datetime.now(UTC)
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
    repo = UserReactionRepository(db)
    existing = repo.find(doc.id, body.reaction)
    if existing and body.notes is None:
        db.delete(existing)
    else:
        repo.set_reaction(doc.id, body.reaction, body.notes, user_id=user.id)
    db.commit()
    return [
        ReactionView(reaction=r.reaction, notes=r.notes, created_at=r.ingest_date)
        for r in repo.get_by_document(doc.id)
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


@router.patch("/action-items/{item_id}", response_model=ActionView)
def set_action_status(
    body: ActionStatusUpdate,
    db: Session = Depends(get_db),
    item: ActionItem = Depends(require_action_item_access(edit=True)),
):
    item.status = body.status
    db.commit()
    return ActionView(
        id=item.id,
        title=item.title,
        description=item.description,
        due_date=item.due_date,
        action_type=item.action_type,
        status=item.status,
        location=item.location,
        addressee=item.addressee,
    )


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
    return _case_ref(case)


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
