import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.access_guards import (
    require_action_item_access,
    require_document_access,
)
from app.config import templates
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import Document, User
from app.models.enums import UserReactionType
from app.repositories.user_reaction import UserReactionRepository
from app.services.case_dashboard_service import summary_bullets_from_ai_summary
from app.services.pipeline_status import stages_dict
from app.services.triage_retry import dispatch_pipeline_retry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])


@router.delete("/document/{doc_id}")
async def delete_document(
    doc_id: int,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Delete a document and its associated file."""
    from app.services.document_service import DocumentService

    if not DocumentService(db).delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return HTMLResponse("")


@router.post("/document/{doc_id}/reaction")
async def hud_toggle_reaction(
    request: Request,
    doc_id: int,
    reaction: str = Form(...),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access(edit=True)),
):
    import json as _json

    try:
        reaction_enum = UserReactionType(reaction)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Unknown reaction: {reaction}"
        ) from exc

    repo = UserReactionRepository(db)
    existing = repo.find(doc_id, reaction_enum)
    if existing and notes is None:
        db.delete(existing)
    else:
        repo.set_reaction(doc_id, reaction_enum, notes, user_id=user.id)
    db.commit()

    reactions = list(repo.get_by_document(doc_id))
    response = templates.TemplateResponse(
        request,
        "partials/hud/_reactions.html",
        {"doc": doc, "reactions": reactions},
    )

    if notes is not None and notes.strip():
        from app.helpers import toast_trigger

        response.headers["HX-Trigger"] = _json.dumps(
            toast_trigger("Note saved", "success")
        )

    return response


@router.post("/document/{doc_id}/hud/approve-summary")
async def hud_approve_summary(
    request: Request,
    doc_id: int,
    action: str,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    if action == "approve":
        doc.ai_summary_approved_at = datetime.now()
    elif action == "reject":
        doc.ai_summary = None
        doc.ai_summary_approved_at = None
    else:
        raise HTTPException(status_code=422, detail=f"Unknown action: {action}")

    db.commit()
    db.refresh(doc)
    summary_bullets = summary_bullets_from_ai_summary(doc.ai_summary)
    return templates.TemplateResponse(
        request,
        "partials/hud/_summary.html",
        {"doc": doc, "summary_bullets": summary_bullets},
    )


# ---------------------------------------------------------------------------
# Pipeline status endpoints
# ---------------------------------------------------------------------------


@router.get("/document/{doc_id}/pipeline")
def get_pipeline_status(
    request: Request,
    doc_id: int,
    view: str = "pill",
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access()),
):
    """Return the rendered pipeline status partial (pill or stepper)."""
    template = (
        "partials/_pipeline_stepper.html"
        if view == "stepper"
        else "partials/_pipeline_pill.html"
    )
    return templates.TemplateResponse(request, template, {"doc": doc})


@router.post("/document/{doc_id}/pipeline/{stage}/retry")
@limiter.limit("30/minute")
async def retry_pipeline_stage(
    request: Request,
    doc_id: int,
    stage: str,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Retry a specific pipeline stage. Returns 409 if upstream is running."""
    from app.models.enums import PipelineStage
    from app.services.pipeline_status import (
        STAGE_REGISTRY,
        get_upstream_blocking,
        reset_stage,
    )

    try:
        pipeline_stage = PipelineStage(stage)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown stage: {stage}") from exc

    # Row-lock this document before reading stages so a concurrent worker
    # can't mark_started between our guard check and reset_stage.
    _lock_row_for_retry(doc_id, db)
    db.refresh(doc)
    stages = stages_dict(doc)

    # Guard: reject if this stage itself is running or already scheduled to
    # retry on its own (RETRYING) — resetting+redispatching over a live
    # RETRYING countdown races the two dispatches. A lost RETRYING row is
    # reclaimed by the PR3a orphan sweep, so this is safe to block on.
    current = stages.get(stage, {}).get("status")
    if current in ("running", "retrying"):
        return templates.TemplateResponse(
            request,
            "partials/_pipeline_stepper.html",
            {"doc": doc, "retry_error": f"Stage '{stage}' is already {current}."},
            status_code=409,
        )

    # Guard: reject if any upstream stage is running. Message reflects that
    # the cascade will re-run this stage when upstream finishes — each task's
    # tail calls claim_stage_for_dispatch on its successors.
    blocking = get_upstream_blocking(pipeline_stage, stages)
    if blocking:
        stage_label = STAGE_REGISTRY[pipeline_stage].label
        upstream_labels = [STAGE_REGISTRY[PipelineStage(b)].label for b in blocking]
        upstream_str = ", ".join(upstream_labels)
        if len(upstream_labels) == 1:
            tail = "when it completes"
        else:
            tail = "when they complete"
        return templates.TemplateResponse(
            request,
            "partials/_pipeline_stepper.html",
            {
                "doc": doc,
                "retry_error": (
                    f"Waiting for {upstream_str} — {stage_label} will re-run "
                    f"automatically {tail}."
                ),
            },
            status_code=409,
        )

    # Reset stage (and dependents) to PENDING and dispatch the appropriate task.
    #
    # BATCH_ANALYSIS is batch-shared: analyze_batch_task marks every sibling
    # document's batch_analysis FAILED simultaneously on terminal failure (it's
    # one shared analysis run for the whole batch, not independent per-doc
    # attempts), so after a failure every sibling is normally FAILED together.
    # claim_batch_for_analysis's readiness predicate requires NONE of them to
    # be terminal — resetting only this one doc would leave every other
    # sibling still FAILED, so the claim inside dispatch_pipeline_retry would
    # never succeed and nothing would ever get dispatched. Reset every FAILED
    # sibling, matching what a batch-level retry converges to.
    if pipeline_stage == PipelineStage.BATCH_ANALYSIS and doc.ingest_batch_id:
        siblings = (
            db.query(Document)
            .filter(Document.ingest_batch_id == doc.ingest_batch_id)
            .all()
        )
        for sibling in siblings:
            if stages_dict(sibling).get("batch_analysis", {}).get("status") == "failed":
                reset_stage(sibling.id, PipelineStage.BATCH_ANALYSIS, db)
    else:
        reset_stage(doc_id, pipeline_stage, db)
    db.refresh(doc)

    dispatch_pipeline_retry(doc.id, doc.ingest_batch_id, pipeline_stage, db)

    return templates.TemplateResponse(
        request,
        "partials/_pipeline_stepper.html",
        {"doc": doc},
    )


@router.post("/document/{doc_id}/pipeline/retry-all")
@limiter.limit("30/minute")
async def retry_pipeline_all(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Reset every non-skipped stage to PENDING and re-dispatch from EXTRACT.

    Returns the refreshed stepper. 409 if any stage is currently RUNNING — the
    user has to wait for the in-flight task to finish before retrying.
    """
    from app.models.enums import PipelineStage, StageStatus
    from app.services.pipeline_status import reset_all_stages, retry_on_db_locked

    def _do_reset():
        # Row-lock this document before reading stages so a concurrent worker
        # can't mark_started between the guard check and reset_all_stages.
        _lock_row_for_retry(doc_id, db)
        db.refresh(doc)
        stages = stages_dict(doc)
        # RETRYING is included alongside RUNNING: a stage with its own
        # scheduled retry countdown still in flight would otherwise race
        # reset_all_stages' PENDING reset against that countdown firing.
        running_stages = [
            key
            for key, val in stages.items()
            if isinstance(val, dict)
            and val.get("status")
            in (StageStatus.RUNNING.value, StageStatus.RETRYING.value)
        ]
        if running_stages:
            return running_stages
        reset_all_stages(doc_id, db)  # commits internally
        return []

    try:
        running = retry_on_db_locked(_do_reset, db)
    except OperationalError:
        return templates.TemplateResponse(
            request,
            "partials/_pipeline_stepper.html",
            {"doc": doc, "retry_error": "Worker busy — try again in a moment"},
            status_code=409,
        )

    if running:
        return templates.TemplateResponse(
            request,
            "partials/_pipeline_stepper.html",
            {
                "doc": doc,
                "retry_error": (
                    "Cannot retry — stage(s) still running: " + ", ".join(running)
                ),
            },
            status_code=409,
        )

    # Clear the per-stage reload latch on the parent batch so the triage row
    # re-renders when each major stage finishes after this retry.
    if doc.ingest_batch_id:
        from app.models.database import IngestBatch

        batch = (
            db.query(IngestBatch).filter(IngestBatch.id == doc.ingest_batch_id).first()
        )
        if batch is not None and batch.meta:
            meta = dict(batch.meta)
            meta.pop("reload_fired", None)
            batch.meta = meta
            db.commit()

    db.refresh(doc)

    # Kick off the pipeline from EXTRACT — process_document_task chains forward
    # to METADATA → PROCEEDING_ANALYSIS → ENRICH → … and dispatches EMBEDDINGS
    # in parallel, so a single dispatch covers every non-skipped stage.
    dispatch_pipeline_retry(doc.id, doc.ingest_batch_id, PipelineStage.EXTRACT, db)

    return templates.TemplateResponse(
        request, "partials/_pipeline_stepper.html", {"doc": doc}
    )


def _lock_row_for_retry(doc_id: int, db: Session) -> None:
    """Take a row-level lock on this document before reading its stage rows.

    `SELECT ... FOR UPDATE` blocks any other transaction from locking or
    updating this same row until we commit/rollback, so subsequent reads in
    this transaction are guaranteed current. Closes the read-check-write race
    in the retry endpoints — without this, a Celery worker could mark_started
    on an upstream stage between the guard check and reset_stage.
    """
    from sqlalchemy import text

    db.execute(
        text("SELECT id FROM documents WHERE id = :doc_id FOR UPDATE"),
        {"doc_id": doc_id},
    )


# ---------------------------------------------------------------------------
# Margin pins — passage-anchored annotations.
# ---------------------------------------------------------------------------


@router.patch("/action-item/{item_id}/status")
async def update_action_item_status(
    request: Request,
    item_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
    item=Depends(require_action_item_access(edit=True)),
):
    """Update an action item's status (open / done / dismissed)."""
    from app.models.enums import ActionItemStatus

    try:
        item.status = ActionItemStatus(status)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Unknown status: {status}"
        ) from exc

    db.commit()
    return HTMLResponse(status_code=204, content="")
