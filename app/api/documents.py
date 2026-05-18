import logging
import os
from datetime import datetime
from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload

from app.config import templates
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Case, Document
from app.models.enums import IngestBatchSourceType, UserReactionType
from app.repositories.document_pin import DocumentPinRepository
from app.repositories.user_reaction import UserReactionRepository
from app.services.case_dashboard_service import summary_bullets_from_ai_summary
from app.services.hud_context import build_hud_context
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.service import (
    create_manual_upload_batch,
    ingest_file,
)
from app.services.pipeline_status import stages_dict
from app.services.triage_retry import dispatch_pipeline_retry
from app.tasks.document_processing import process_document_task

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])


@router.get("/upload")
async def upload_page(request: Request, db: Session = Depends(get_db)):
    case_id = request.query_params.get("case_id")
    case = db.query(Case).filter(Case.id == case_id).first() if case_id else None

    top_level_docs = []
    if case_id:
        top_level_docs = (
            db.query(Document)
            .filter(Document.case_id == case_id, Document.parent_id.is_(None))
            .all()
        )

    context = {
        "case_id": case_id,
        "case_title": case.title if case else None,
        "top_level_docs": top_level_docs,
    }

    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "partials/upload_form.html", context)

    return render_page(request, "partials/upload_form.html", db=db, **context)


@router.post("/upload")
async def upload_document(
    request: Request,
    db: Session = Depends(get_db),
):
    form = await request.form()
    files = form.getlist("files")
    case_id_raw = form.get("case_id")
    case_id = case_id_raw if case_id_raw else None
    parent_id_raw = form.get("parent_id")
    parent_id = int(parent_id_raw) if parent_id_raw else None

    if not files or all(not f.filename for f in files):
        if request.headers.get("hx-request"):
            return HTMLResponse(
                '<div class="p-3 text-sm text-error">No files selected.</div>',
                status_code=400,
            )
        return JSONResponse({"error": "No files selected."}, status_code=400)

    results = []
    success_count = 0
    error_count = 0

    valid_files = [f for f in files if f.filename]

    # Non-EML files share a single manual batch; EML files create their own batch
    # via ingest_raw_email (same path as Gmail import).
    non_eml_files = [
        f for f in valid_files if os.path.splitext(f.filename)[1].lower() != ".eml"
    ]
    ingest_batch_id = None
    if non_eml_files:
        ingest_batch_id = create_manual_upload_batch(
            db,
            filenames=[f.filename for f in non_eml_files],
            case_id=case_id,
        )
        db.commit()

    def _row_queued(filename: str, doc_id: int | None = None, sub: str = "") -> str:
        filename = escape(filename)
        sub = escape(sub)
        # Each row carries a polling probe that swaps itself with the latest
        # status from /upload/status/{doc_id} every 2 s while in-flight. The
        # triage queue is the canonical "watch it run" view, but having live
        # state in the modal closes the dead-zone before the user navigates.
        probe = (
            f' hx-get="/upload/status/{doc_id}" hx-trigger="every 2s"'
            f' hx-swap="outerHTML"'
            if doc_id
            else ""
        )
        sub_html = (
            f'<p class="text-[9px] text-on-surface-variant">{sub}</p>' if sub else ""
        )
        return (
            f'<div class="flex items-start gap-2 px-2 py-1.5 rounded bg-originator-own/5 border border-originator-own/15"{probe}>'
            f'<span class="material-symbols-outlined text-[14px] text-originator-own animate-spin">progress_activity</span>'
            f'<div class="flex-1 min-w-0"><p class="text-xs font-bold text-on-surface truncate" title="{filename}">{filename}</p>'
            f'<p class="text-[10px] text-on-surface-variant">queued for processing</p>'
            f"{sub_html}</div></div>"
        )

    def _row_dup(filename: str) -> str:
        filename = escape(filename)
        return (
            f'<div class="flex items-start gap-2 px-2 py-1.5 rounded bg-surface-container/40 border border-outline-variant/10">'
            f'<span class="material-symbols-outlined text-[14px] text-on-surface-variant">history</span>'
            f'<div class="flex-1 min-w-0"><p class="text-xs font-bold text-on-surface-variant truncate" title="{filename}">{filename}</p>'
            f'<p class="text-[10px] text-on-surface-variant">already ingested</p></div></div>'
        )

    def _row_error(filename: str, msg: str) -> str:
        filename = escape(filename)
        msg = escape(msg)
        return (
            f'<div class="flex items-start gap-2 px-2 py-1.5 rounded bg-error-container/15 border border-error/20">'
            f'<span class="material-symbols-outlined text-[14px] text-error">error</span>'
            f'<div class="flex-1 min-w-0"><p class="text-xs font-bold text-on-surface truncate" title="{filename}">{filename}</p>'
            f'<p class="text-[10px] text-error truncate" title="{msg}">{msg}</p></div></div>'
        )

    for file in files:
        if not file.filename:
            continue

        ext = os.path.splitext(file.filename)[1].lower()

        if ext == ".eml":
            # Route through the unified email ingestion path — same as Gmail import.
            # No Document is created for the .eml envelope itself.
            try:
                raw_bytes = await file.read()
                batch = ingest_raw_email(
                    db, raw_bytes, source_type=IngestBatchSourceType.MANUAL
                )
                if batch:
                    success_count += 1
                    results.append(_row_queued(file.filename, sub=f"batch #{batch.id}"))
                else:
                    results.append(_row_dup(file.filename))
            except Exception as e:
                error_count += 1
                logger.error(
                    f"EML ingest failed for {file.filename}: {e}", exc_info=True
                )
                results.append(_row_error(file.filename, str(e)))
            continue

        try:
            doc = await ingest_file(
                file,
                case_id,
                db,
                parent_id,
                skip_processing=True,
                ingest_batch_id=ingest_batch_id,
            )
            success_count += 1

            _doc_id = doc.id
            from app.tasks.dispatch import dispatch_task

            dispatch_task(process_document_task, _doc_id)

            results.append(_row_queued(file.filename, doc_id=_doc_id))

        except HTTPException as e:
            error_count += 1
            results.append(_row_error(file.filename, str(e.detail)))
        except Exception as e:
            error_count += 1
            logger.error(f"Upload failed for file {file.filename}: {e}", exc_info=True)
            results.append(_row_error(file.filename, f"Upload failed: {e}"))

    if success_count == 0 and error_count > 0:
        return HTMLResponse(
            f"<div class='space-y-1.5'>{''.join(results)}</div>",
            status_code=400,
        )

    if request.headers.get("hx-request"):
        summary = (
            f"<div class='flex items-center gap-2 text-xs mb-2'>"
            f"<span class='font-black text-on-surface'>{success_count}</span> "
            f"<span class='text-on-surface-variant'>uploaded</span>"
            + (
                f", <span class='font-black text-error'>{error_count}</span> "
                f"<span class='text-on-surface-variant'>failed</span>"
                if error_count
                else ""
            )
            + "</div>"
        )
        return HTMLResponse(
            summary + f"<div class='space-y-1.5'>{''.join(results)}</div>"
        )

    return {
        "results": results,
        "success_count": success_count,
        "error_count": error_count,
    }


@router.post("/documents/bulk-delete")
async def bulk_delete_documents(request: Request, db: Session = Depends(get_db)):
    """Delete multiple documents and their associated files."""
    from app.services.document_service import DocumentService

    form = await request.form()
    doc_ids = form.getlist("doc_ids")

    if not doc_ids:
        return HTMLResponse("", status_code=200)

    doc_service = DocumentService(db)
    success_count = 0
    for doc_id_str in doc_ids:
        try:
            if doc_service.delete_document(int(doc_id_str)):
                success_count += 1
        except Exception as e:
            logger.error(f"Bulk delete failed for doc {doc_id_str}: {e}")

    return HTMLResponse(
        '<div hx-trigger="load" hx-get="/triage" hx-target="body"></div>',
        status_code=200,
    )


@router.get("/upload/status/{doc_id}")
async def upload_status_row(doc_id: int, db: Session = Depends(get_db)):
    """Self-replacing status row for the upload modal's per-file probe.

    Polls every 2 s from the row in /upload's response. While the doc's
    pipeline is in pending/running, returns the same in-flight row (with
    current stage label). When the pipeline reaches a terminal state, returns
    a final row that disarms the polling (no hx-* attributes).
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        # Row was deleted under us — return empty so the polling probe stops.
        return HTMLResponse("")

    state = doc.pipeline_state.value if doc.pipeline_state else "pending"
    filename = escape(doc.title or "(untitled)")

    if state == "failed":
        # Find the first failed stage for the error message.
        failed_stage = ""
        failed_error = ""
        for stage_key, stage_rec in stages_dict(doc).items():
            if isinstance(stage_rec, dict) and stage_rec.get("status") == "failed":
                failed_stage = stage_key
                failed_error = stage_rec.get("error") or ""
                break
        msg = (
            f"{failed_stage.replace('_', ' ')} failed"
            if failed_stage
            else "pipeline failed"
        )
        if failed_error:
            msg += f" — {failed_error[:80]}"
        msg = escape(msg)
        return HTMLResponse(
            f'<div class="flex items-start gap-2 px-2 py-1.5 rounded bg-error-container/15 border border-error/20">'
            f'<span class="material-symbols-outlined text-[14px] text-error">error</span>'
            f'<div class="flex-1 min-w-0"><p class="text-xs font-bold text-on-surface truncate" title="{filename}">{filename}</p>'
            f'<p class="text-[10px] text-error truncate" title="{msg}">{msg}</p></div></div>'
        )

    if state == "completed":
        return HTMLResponse(
            f'<div class="flex items-start gap-2 px-2 py-1.5 rounded bg-originator-own/10 border border-originator-own/30">'
            f'<span class="material-symbols-outlined text-[14px] text-originator-own">check_circle</span>'
            f'<div class="flex-1 min-w-0"><p class="text-xs font-bold text-on-surface truncate" title="{filename}">{filename}</p>'
            f'<p class="text-[10px] text-originator-own">ready</p></div></div>'
        )

    # In-flight: prefer a retrying stage over a running one — when an attempt
    # just failed and the next is queued, that's the signal the user wants.
    retrying_stage = ""
    retrying_attempt = None
    retrying_max = None
    retrying_next_at = ""
    running_stage = ""
    for stage_key, stage_rec in stages_dict(doc).items():
        if not isinstance(stage_rec, dict):
            continue
        st = stage_rec.get("status")
        if st == "retrying" and not retrying_stage:
            retrying_stage = stage_key
            retrying_attempt = stage_rec.get("attempt")
            retrying_max = stage_rec.get("max_attempts")
            retrying_next_at = stage_rec.get("next_at") or ""
        elif st == "running" and not running_stage:
            running_stage = stage_key

    if retrying_stage:
        parts = [f"retrying {retrying_stage.replace('_', ' ')}"]
        if retrying_attempt and retrying_max:
            parts.append(f"({retrying_attempt}/{retrying_max})")
        if retrying_next_at:
            parts.append(f"· next {escape(retrying_next_at[11:19])}")
        label = " ".join(parts)
    elif running_stage:
        label = f"{running_stage.replace('_', ' ')}…"
    else:
        label = "queued for processing"
    return HTMLResponse(
        f'<div class="flex items-start gap-2 px-2 py-1.5 rounded bg-originator-own/5 border border-originator-own/15"'
        f' hx-get="/upload/status/{doc_id}" hx-trigger="every 2s" hx-swap="outerHTML">'
        f'<span class="material-symbols-outlined text-[14px] text-originator-own animate-spin">progress_activity</span>'
        f'<div class="flex-1 min-w-0"><p class="text-xs font-bold text-on-surface truncate" title="{filename}">{filename}</p>'
        f'<p class="text-[10px] text-on-surface-variant">{label}</p></div></div>'
    )


@router.delete("/document/{doc_id}")
async def delete_document(
    request: Request,
    doc_id: int,
    context: str | None = None,
    db: Session = Depends(get_db),
):
    """Delete a document and its associated file."""
    from app.services.document_service import DocumentService

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    bundle_key = None
    if context == "triage":
        if doc.ingest_batch_id:
            bundle_key = f"batch-{doc.ingest_batch_id}"
        else:
            bundle_key = f"loose-{doc.id}"

    # Identify the next document to advance to before we delete the current one.
    next_doc_id = None
    if context == "triage":
        from app.services.triage_service import TriageService

        triage_service = TriageService(db)
        next_doc = triage_service.find_next_review_doc(doc_id)
        if next_doc:
            next_doc_id = next_doc.id

    doc_service = DocumentService(db)
    if not doc_service.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")

    # Deleting a doc may orphan its draft case (the last doc on the draft just
    # left). Sweep here so the picker / status counts stay honest.
    if context == "triage":
        from app.services.triage_service import TriageService as _TS

        _TS(db).cleanup_orphaned_drafts()

    if context == "triage" and bundle_key:
        import json

        from app.services.triage_oob_render import (
            render_bundle_group_oob,
            render_sidebar_badges_oob,
            render_triage_feed_oob,
            render_triage_header_stats_oob,
        )

        triage_service = TriageService(db)
        bundles = triage_service.get_triage_bundles()

        trigger = {}
        if next_doc_id:
            trigger["triage:advance"] = {"next_doc_id": next_doc_id, "scroll": False}
        else:
            trigger["triage:clear"] = {}

        # Global synchronization: Sidebar badges and Triage status bar
        global_oob = render_sidebar_badges_oob(db)
        global_oob += render_triage_header_stats_oob(request, triage_service)

        if not bundles:
            # Entire queue is now empty — swap the full feed to show empty state message.
            res_content = render_triage_feed_oob(request, triage_service, db)
            res_content += global_oob
            response = HTMLResponse(res_content)
        else:
            bundle = next((b for b in bundles if b.key == bundle_key), None)
            if bundle:
                # Bundle still has documents — return the updated bundle group OOB.
                res_content = render_bundle_group_oob(request, bundle, triage_service)
                res_content += global_oob
                response = HTMLResponse(res_content)
            else:
                # This bundle is now empty, but others remain — delete the group from DOM.
                res_content = (
                    f'<div id="triage-row-{bundle_key}" hx-swap-oob="delete"></div>'
                )
                res_content += global_oob
                response = HTMLResponse(res_content)

        response.headers["HX-Trigger"] = json.dumps(trigger)
        return response

    return HTMLResponse("", status_code=200)


@router.get("/document/{doc_id}")
async def document_detail(
    request: Request,
    doc_id: int,
    context: str | None = None,
    db: Session = Depends(get_db),
):
    doc = (
        db.query(Document)
        .options(joinedload(Document.proceeding))
        .filter(Document.id == doc_id)
        .first()
    )
    if not doc:
        return templates.TemplateResponse(
            request,
            "errors/404.html",
            {"message": f"Document {doc_id} not found"},
            status_code=404,
        )

    if request.headers.get("hx-request"):
        mode = "review" if context == "triage" else "read"
        # Pass the case picker list whenever the metadata form is rendered
        # (review mode) so the <select> has options. The in-context draft
        # gets prepended by build_hud_context.
        cases = None
        if mode == "review":
            from app.models.database import Case as _Case

            cases = (
                db.query(_Case)
                .filter(_Case.id != "_TRIAGE", _Case.is_draft.is_(False))
                .order_by(_Case.title.asc())
                .all()
            )
        ctx = build_hud_context(db, doc, mode=mode, context="embedded", cases=cases)
        return templates.TemplateResponse(request, "partials/hud/_container.html", ctx)

    # Full-page navigations: case docs redirect to the canonical URL (which
    # renders pages/document.html). Triage docs render it directly since they
    # have no canonical /cases/… URL yet.
    if not doc.case_id or doc.case_id == "_TRIAGE":
        from app.helpers import render_page

        ctx = build_hud_context(db, doc, mode="read")
        ctx["context"] = "standalone"
        ctx["case_id"] = "_TRIAGE"
        return render_page(request, "pages/document.html", db=db, **ctx)
    return RedirectResponse(
        url=f"/cases/{doc.case_id}/document/{doc.id}", status_code=302
    )


@router.post("/document/{doc_id}/reaction")
async def hud_toggle_reaction(
    request: Request,
    doc_id: int,
    reaction: str = Form(...),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
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
        repo.set_reaction(doc_id, reaction_enum, notes)
    db.commit()

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
    reactions = list(repo.get_by_document(doc_id))
    response = templates.TemplateResponse(
        request,
        "partials/hud/_reactions.html",
        {"doc": doc, "reactions": reactions},
    )

    # OOB row refresh for triage (selector misses gracefully outside triage)
    from app.services.triage_oob_render import render_row_targeted_oob
    from app.services.triage_service import TriageService

    triage_service = TriageService(db)
    response.body += render_row_targeted_oob(request, doc, triage_service, db).encode()

    if notes is not None and notes.strip():
        response.headers["HX-Trigger"] = _json.dumps(
            {"triage:note-saved": {"message": "Note saved"}}
        )

    return response


@router.post("/document/{doc_id}/hud/approve-summary")
async def hud_approve_summary(
    request: Request,
    doc_id: int,
    action: str,
    db: Session = Depends(get_db),
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

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
):
    """Return the rendered pipeline status partial (pill or stepper)."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

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
):
    """Retry a specific pipeline stage. Returns 409 if upstream is running."""
    from app.models.enums import PipelineStage
    from app.services.pipeline_status import get_upstream_blocking, reset_stage

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    try:
        pipeline_stage = PipelineStage(stage)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Unknown stage: {stage}") from exc

    # Take SQLite writer lock on this row before reading stages so a concurrent
    # worker can't mark_started between our guard check and reset_stage.
    _lock_row_for_retry(doc_id, db)
    db.refresh(doc)
    stages = stages_dict(doc)

    # Guard: reject if this stage itself is running
    current = stages.get(stage, {}).get("status")
    if current == "running":
        return templates.TemplateResponse(
            request,
            "partials/_pipeline_stepper.html",
            {"doc": doc, "retry_error": f"Stage '{stage}' is already running."},
            status_code=409,
        )

    # Guard: reject if any upstream stage is running
    blocking = get_upstream_blocking(pipeline_stage, stages)
    if blocking:
        return templates.TemplateResponse(
            request,
            "partials/_pipeline_stepper.html",
            {
                "doc": doc,
                "retry_error": f"Cannot retry '{stage}' — upstream stage(s) running: {', '.join(blocking)}",
            },
            status_code=409,
        )

    # Reset stage (and dependents) to PENDING and dispatch the appropriate task
    reset_stage(doc_id, pipeline_stage, db)
    db.refresh(doc)

    dispatch_pipeline_retry(doc.id, doc.ingest_batch_id, pipeline_stage)

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
):
    """Reset every non-skipped stage to PENDING and re-dispatch from EXTRACT.

    Returns the refreshed stepper. 409 if any stage is currently RUNNING — the
    user has to wait for the in-flight task to finish before retrying.
    """
    from app.models.enums import PipelineStage, StageStatus
    from app.services.pipeline_status import reset_all_stages, retry_on_db_locked

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    def _do_reset():
        # Take SQLite writer lock before reading stages so a concurrent worker
        # can't mark_started between the guard check and reset_all_stages.
        _lock_row_for_retry(doc_id, db)
        db.refresh(doc)
        stages = stages_dict(doc)
        running_stages = [
            key
            for key, val in stages.items()
            if isinstance(val, dict) and val.get("status") == StageStatus.RUNNING.value
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
    dispatch_pipeline_retry(doc.id, doc.ingest_batch_id, PipelineStage.EXTRACT)

    return templates.TemplateResponse(
        request, "partials/_pipeline_stepper.html", {"doc": doc}
    )


def _lock_row_for_retry(doc_id: int, db: Session) -> None:
    """Acquire SQLite's writer lock on a documents row via a no-op UPDATE.

    SQLite serializes write transactions, so subsequent reads in the same
    transaction see the latest committed state and no other writer can change
    the row until we commit. Closes the read-check-write race in the retry
    endpoints — without this, a Celery worker could mark_started on an upstream
    stage between the guard check and reset_stage.
    """
    from sqlalchemy import text

    db.execute(
        text("UPDATE documents SET id = id WHERE id = :doc_id"),
        {"doc_id": doc_id},
    )


# ---------------------------------------------------------------------------
# Margin pins — passage-anchored annotations.
# ---------------------------------------------------------------------------


@router.post("/document/{doc_id}/pin")
async def create_pin(
    request: Request,
    doc_id: int,
    passage_id: str = Form(...),
    note: str | None = Form(None),
    db: Session = Depends(get_db),
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    repo = DocumentPinRepository(db)
    pin = repo.create(doc_id, passage_id, note)
    db.commit()
    db.refresh(pin)

    pins = repo.get_by_document(doc_id)
    passage_pin_counts: dict[str, int] = {}
    for p in pins:
        passage_pin_counts[p.passage_id] = passage_pin_counts.get(p.passage_id, 0) + 1

    return templates.TemplateResponse(
        request,
        "partials/hud/_pin_card.html",
        {"pin": pin, "passage_pin_counts": passage_pin_counts},
    )


@router.patch("/pin/{pin_id}")
async def update_pin(
    pin_id: int,
    note: str | None = Form(None),
    db: Session = Depends(get_db),
):
    repo = DocumentPinRepository(db)
    pin = repo.update_note(pin_id, note)
    if pin is None:
        raise HTTPException(status_code=404, detail=f"Pin {pin_id} not found")
    db.commit()
    return HTMLResponse("", status_code=204)


@router.delete("/pin/{pin_id}")
async def delete_pin(pin_id: int, db: Session = Depends(get_db)):
    repo = DocumentPinRepository(db)
    if not repo.delete(pin_id):
        raise HTTPException(status_code=404, detail=f"Pin {pin_id} not found")
    db.commit()
    return HTMLResponse("", status_code=200)


# ---------------------------------------------------------------------------
# Original file — serve raw stored file in a new tab.
# ---------------------------------------------------------------------------


@router.patch("/action-item/{item_id}/status")
async def update_action_item_status(
    request: Request,
    item_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    """Update an action item's status (open / done / dismissed)."""
    from app.models.database import ActionItem
    from app.models.enums import ActionItemStatus

    item = db.get(ActionItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")

    try:
        item.status = ActionItemStatus(status)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Unknown status: {status}"
        ) from exc

    db.commit()
    return HTMLResponse(status_code=204, content="")


@router.post("/document/{doc_id}/cost-from-delta")
async def promote_cost_delta(
    request: Request,
    doc_id: int,
    category_override: str | None = Form(None),
    vat_rate_override: float | None = Form(None),
    db: Session = Depends(get_db),
):
    """Promote doc.cost_delta into a LegalCost row and redirect to cost form."""
    from app.models.database import LegalCost
    from app.models.enums import CostCategory
    from app.services.case_service import recompute_total_cost_exposure

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not doc.cost_delta or not doc.case_id:
        raise HTTPException(status_code=422, detail="No cost delta or case to promote")

    cd = doc.cost_delta if isinstance(doc.cost_delta, dict) else {}
    amount = float(cd.get("amount") or 0)
    direction = cd.get("direction") or "none"
    description = cd.get("description") or doc.title or "Cost from document"

    # Statutary defaults: lawyer (outgoing) has VAT, court (incoming/ruling) does not
    category = CostCategory.SONSTIGES
    if category_override:
        try:
            category = CostCategory(category_override)
        except ValueError:
            pass

    vat_rate = 0.0
    if vat_rate_override is not None:
        vat_rate = vat_rate_override
    elif direction == "outgoing":
        vat_rate = 0.19

    amount_gross = amount * (1 + vat_rate)

    cost = LegalCost(
        case_id=doc.case_id,
        proceeding_id=doc.proceeding_id,
        category=category,
        title=description,
        amount_net=amount,
        vat_rate=vat_rate,
        amount_gross=amount_gross,
        source_document_id=doc.id,
        issued_at=doc.issued_date or doc.ingest_date,
    )
    db.add(cost)
    db.commit()
    db.refresh(cost)

    recompute_total_cost_exposure(doc.case_id, db)

    return HTMLResponse(
        '<span class="text-[10px] text-originator-own font-bold">✓ promoted</span>',
        status_code=200,
    )


@router.get("/document/{doc_id}/original")
async def document_original(
    doc_id: int,
    db: Session = Depends(get_db),
):
    from app.config import DATA_DIR

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    if not doc.file_path:
        raise HTTPException(
            status_code=404, detail="No original file stored for this document"
        )

    import pathlib

    file_path = pathlib.Path(doc.file_path)
    if not file_path.is_absolute():
        file_path = DATA_DIR / file_path

    # Defense-in-depth: refuse to serve anything outside DATA_DIR even if the
    # stored file_path were ever attacker-influenced (compromised task,
    # malicious migration, future SQLi).
    resolved = file_path.resolve()
    data_root = DATA_DIR.resolve()
    if not str(resolved).startswith(str(data_root) + "/") and resolved != data_root:
        raise HTTPException(status_code=404, detail="Original file not found on disk")

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Original file not found on disk")

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
