import logging
import os
from datetime import datetime
from html import escape
from typing import cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi import UploadFile as FastAPIUploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.access_guards import (
    check_owned_or_case_access,
    require_action_item_access,
    require_document_access,
    require_pin_access,
)
from app.config import templates
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.helpers import render_page
from app.models.database import Case, Document, IngestBatch, User
from app.models.enums import IngestBatchSourceType, UserReactionType
from app.repositories.document_pin import DocumentPinRepository
from app.repositories.user_reaction import UserReactionRepository
from app.services.case_dashboard_service import summary_bullets_from_ai_summary
from app.services.hud_context import build_hud_context
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.converters import MAX_FILE_SIZE
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
@limiter.limit("60/minute")
async def upload_document(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    form = await request.form()
    files = [f for f in form.getlist("files") if isinstance(f, StarletteUploadFile)]
    case_id_raw = form.get("case_id")
    case_id = case_id_raw if isinstance(case_id_raw, str) and case_id_raw else None
    parent_id_raw = form.get("parent_id")
    parent_id = (
        int(parent_id_raw) if isinstance(parent_id_raw, str) and parent_id_raw else None
    )

    # Uploading straight into an existing case requires edit rights on it —
    # otherwise any authenticated user could add documents to a case they
    # can't even view. Uploads with no case_id (or "_TRIAGE") land in the
    # uploader's own triage queue, which needs no case check.
    if case_id and case_id != "_TRIAGE":
        from app.models.database import Case as _Case
        from app.services import access_service

        target_case = db.query(_Case).filter(_Case.id == case_id).first()
        if not access_service.can_edit_case(db, user, target_case):
            raise HTTPException(status_code=404, detail="Case not found")

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
        f
        for f in valid_files
        if os.path.splitext(cast(str, f.filename))[1].lower() != ".eml"
    ]
    ingest_batch_id = None
    if non_eml_files:
        ingest_batch_id = create_manual_upload_batch(
            db,
            filenames=[cast(str, f.filename) for f in non_eml_files],
            case_id=case_id,
            owner_id=user.id,
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
                # Chunked read with a running total, like ingest_file — a bare
                # await file.read() still buffers the entire file in memory
                # before any size check runs.
                chunks = []
                total_size = 0
                while chunk := await file.read(1024 * 1024):
                    total_size += len(chunk)
                    if total_size > MAX_FILE_SIZE:
                        max_mb = MAX_FILE_SIZE // (1024 * 1024)
                        raise ValueError(f"File too large. Maximum size: {max_mb}MB")
                    chunks.append(chunk)
                raw_bytes = b"".join(chunks)
                batch = ingest_raw_email(
                    db,
                    raw_bytes,
                    source_type=IngestBatchSourceType.MANUAL,
                    owner_id=user.id,
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
                results.append(_row_error(file.filename, "Upload failed"))
            continue

        try:
            doc = await ingest_file(
                cast(FastAPIUploadFile, file),
                case_id,
                db,
                parent_id,
                skip_processing=True,
                ingest_batch_id=ingest_batch_id,
                owner_id=user.id,
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
            results.append(_row_error(file.filename, "Upload failed"))

    if ingest_batch_id is not None:
        # create_manual_upload_batch commits the batch row before any of its
        # files are processed above — if every non-EML file in it then fails
        # (duplicate, conversion error, etc.), the row survives with zero
        # documents forever, since delete_bundle has nothing to auto-trigger
        # it. Check actual document count, not success_count: an EML file in
        # the same upload can succeed via its own separate batch (see the
        # ingest_raw_email branch above) without adding to this one.
        remaining = (
            db.query(Document.id)
            .filter(Document.ingest_batch_id == ingest_batch_id)
            .count()
        )
        if remaining == 0:
            db.query(IngestBatch).filter(IngestBatch.id == ingest_batch_id).delete(
                synchronize_session=False
            )
            db.commit()

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


@router.get("/upload/status/{doc_id}")
async def upload_status_row(
    doc_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Self-replacing status row for the upload modal's per-file probe.

    Polls every 2 s from the row in /upload's response. While the doc's
    pipeline is in pending/running, returns the same in-flight row (with
    current stage label). When the pipeline reaches a terminal state, returns
    a final row that disarms the polling (no hx-* attributes).
    """

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not check_owned_or_case_access(
        db, user, owner_id=doc.owner_id, case_id=doc.case_id, edit=False
    ):
        # Row was deleted (or isn't this user's) — return empty so the
        # polling probe stops, same as the not-found case (no 403 leak).
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
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Delete a document and its associated file."""
    from app.services.document_service import DocumentService

    bundle_key = None
    if context == "triage":
        if doc.ingest_batch_id:
            bundle_key = f"batch-{doc.ingest_batch_id}"
        else:
            bundle_key = f"loose-{doc.id}"

    # Render/picker scoping below reflects the *requester's* own access, not
    # the deleted doc's owner — they diverge whenever an admin (or, for a
    # doc in a shared case, an EDITOR-shared user) deletes someone else's
    # document: the OOB response renders into the requester's own browser,
    # so "next doc to review" and the badge/count re-renders must be scoped
    # to what the requester can see, not the person whose doc was deleted.
    requester_id = request.state.current_user.id

    # Identify the next document to advance to before we delete the current one.
    next_doc_id = None
    if context == "triage":
        from app.services.triage_confirmation import find_next_review_doc

        next_doc = find_next_review_doc(db, doc_id, owner_id=requester_id)
        if next_doc:
            next_doc_id = next_doc.id

    doc_service = DocumentService(db)
    if not doc_service.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")

    # Deleting a doc may orphan its draft case (the last doc on the draft just
    # left). Sweep here so the picker / status counts stay honest.
    if context == "triage":
        from app.services.triage_confirmation import cleanup_orphaned_drafts

        cleanup_orphaned_drafts(db)

    if context == "triage" and bundle_key:
        import json

        from app.services.triage_bundles import get_triage_bundles
        from app.services.triage_oob_render import (
            render_bundle_group_oob,
            render_sidebar_badges_oob,
            render_triage_feed_oob,
            render_triage_header_stats_oob,
        )

        bundles = get_triage_bundles(db, owner_id=requester_id)

        trigger = {}
        if next_doc_id:
            trigger["triage:advance"] = {"next_doc_id": next_doc_id, "scroll": False}
        else:
            trigger["triage:clear"] = {}

        # Global synchronization: Sidebar badges and Triage status bar
        global_oob = render_sidebar_badges_oob(db, owner_id=requester_id)
        global_oob += render_triage_header_stats_oob(request, db, owner_id=requester_id)

        if not bundles:
            # Entire queue is now empty — swap the full feed to show empty state message.
            res_content = render_triage_feed_oob(request, db, owner_id=requester_id)
            res_content += global_oob
            response = HTMLResponse(res_content)
        else:
            bundle = next((b for b in bundles if b.key == bundle_key), None)
            if bundle:
                # Bundle still has documents — return the updated bundle group OOB.
                res_content = render_bundle_group_oob(request, bundle, db)
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
    user: User = Depends(get_current_user),
):

    doc = (
        db.query(Document)
        .options(joinedload(Document.proceeding))
        .filter(Document.id == doc_id)
        .first()
    )
    if not doc or not check_owned_or_case_access(
        db, user, owner_id=doc.owner_id, case_id=doc.case_id, edit=False
    ):
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
            from app.repositories.case import CaseRepository

            cases = list(CaseRepository(db).list_for_picker(owner_id=user.id))
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

    # OOB row refresh for triage (selector misses gracefully outside triage)
    from app.services.triage_oob_render import render_row_targeted_oob

    response.body = (
        bytes(response.body) + render_row_targeted_oob(request, doc, db).encode()
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


@router.post("/document/{doc_id}/pin")
async def create_pin(
    request: Request,
    doc_id: int,
    passage_id: str = Form(...),
    note: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access(edit=True)),
):
    repo = DocumentPinRepository(db)
    pin = repo.create(doc_id, passage_id, note, user_id=user.id)
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
    pin=Depends(require_pin_access(edit=True)),
):
    repo = DocumentPinRepository(db)
    repo.update_note(pin_id, note)
    db.commit()
    return HTMLResponse("", status_code=204)


@router.delete("/pin/{pin_id}")
async def delete_pin(
    pin_id: int,
    db: Session = Depends(get_db),
    pin=Depends(require_pin_access(edit=True)),
):
    repo = DocumentPinRepository(db)
    repo.delete(pin_id)
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


@router.post("/document/{doc_id}/cost-from-delta")
async def promote_cost_delta(
    request: Request,
    doc_id: int,
    category_override: str | None = Form(None),
    vat_rate_override: float | None = Form(None),
    amount_override: float | None = Form(None),
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Promote a doc's CostSignal into a LegalCost ledger row.

    Reads the most recent CostSignal for the doc as the basis (amount,
    description), then creates a LegalCost with optional user overrides for
    category, VAT rate, and amount. Invoice/vorschuss kinds already
    auto-materialise to LegalCost during enrichment — those should be edited
    via /costs/{id} rather than promoted here.
    """
    from app.models.database import CostSignal, LegalCost
    from app.models.enums import CostCategory
    from app.services.case_service import recompute_total_cost_exposure

    if not doc.case_id:
        raise HTTPException(status_code=422, detail="Document has no case")

    signal = (
        db.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .order_by(
            CostSignal.issued_at.desc().nullslast(),
            CostSignal.ingest_date.desc(),
        )
        .first()
    )
    if signal is None and amount_override is None:
        raise HTTPException(
            status_code=422,
            detail="No cost signal found for this document and no amount override given",
        )

    if amount_override is not None:
        amount = float(amount_override)
    else:
        # Guaranteed non-None: the guard above raises when both signal and
        # amount_override are None, and amount_override is None in this branch.
        assert signal is not None
        amount = float(signal.amount or 0)
    description = (
        (signal.description if signal else None) or doc.title or "Cost from document"
    )

    category = CostCategory.SONSTIGES
    if category_override:
        try:
            category = CostCategory(category_override)
        except ValueError:
            pass

    vat_rate = vat_rate_override if vat_rate_override is not None else 0.0
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
    doc: Document = Depends(require_document_access()),
):
    from app.config import DATA_DIR
    from app.core.paths import resolve_storage_path

    if not doc.file_path:
        raise HTTPException(
            status_code=404, detail="No original file stored for this document"
        )

    # Defense-in-depth: refuse to serve anything outside DATA_DIR even if the
    # stored file_path were ever attacker-influenced (compromised task,
    # malicious migration, future SQLi).
    resolved = resolve_storage_path(doc.file_path).resolve()
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
