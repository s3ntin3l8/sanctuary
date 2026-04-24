import logging
import os
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.config import templates
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Case, Document
from app.models.enums import IngestBatchSourceType, OriginatorType, UserReactionType
from app.repositories.document_pin import DocumentPinRepository
from app.repositories.user_reaction import UserReactionRepository
from app.services.case_dashboard_service import summary_bullets_from_ai_summary
from app.services.hud_context import build_hud_context, build_triage_hud_context
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.service import (
    create_manual_upload_batch,
    ingest_file,
)
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
        return {"error": "No files selected."}, 400

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
                    results.append(
                        f'<div class="p-2 text-xs text-green-400">✓ {file.filename} — batch #{batch.id} queued</div>'
                    )
                else:
                    results.append(
                        f'<div class="p-2 text-xs text-on-surface-variant">↩ {file.filename} already ingested</div>'
                    )
            except Exception as e:
                error_count += 1
                logger.error(
                    f"EML ingest failed for {file.filename}: {e}", exc_info=True
                )
                results.append(
                    f'<div class="p-2 text-xs text-error">✗ {file.filename}: {e}</div>'
                )
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

            def _dispatch(doc_id: int = _doc_id):
                try:
                    process_document_task.delay(doc_id)
                except Exception as e:
                    logger.warning(f"Celery task dispatch failed for doc {doc_id}: {e}")

            threading.Thread(target=_dispatch, daemon=True).start()

            results.append(
                f'<div class="p-2 text-xs text-green-400">✓ {file.filename} queued for processing</div>'
            )

        except HTTPException as e:
            error_count += 1
            results.append(
                f'<div class="p-2 text-xs text-error">'
                f"✗ {file.filename}: {e.detail}</div>"
            )
        except Exception as e:
            error_count += 1
            logger.error(f"Upload failed for file {file.filename}: {e}", exc_info=True)
            results.append(
                f'<div class="p-2 text-xs text-error">'
                f"✗ {file.filename}: Upload failed: {e}</div>"
            )

    if success_count == 0 and error_count > 0:
        return HTMLResponse(
            f"<div class='space-y-1'>{''.join(results)}</div>",
            status_code=400,
        )

    if request.headers.get("hx-request"):
        summary = f"<div class='p-2 text-xs font-bold text-on-surface'>{success_count} uploaded, {error_count} failed</div>"
        return HTMLResponse(summary + "".join(results))

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

    if context == "triage" and bundle_key:
        import json

        from app.api.triage import (
            _render_bundle_group_oob,
            _render_sidebar_badges_oob,
            _render_triage_feed_oob,
            _render_triage_status_bar_oob,
        )

        triage_service = TriageService(db)
        bundles = triage_service.get_triage_bundles()

        trigger = {}
        if next_doc_id:
            trigger["triage:advance"] = {"next_doc_id": next_doc_id, "scroll": False}
        else:
            trigger["triage:clear"] = {}

        # Global synchronization: Sidebar badges and Triage status bar
        global_oob = _render_sidebar_badges_oob(db)
        global_oob += _render_triage_status_bar_oob(request, triage_service)

        if not bundles:
            # Entire queue is now empty — swap the full feed to show empty state message.
            res_content = _render_triage_feed_oob(request, triage_service, db)
            # Clear the HUD pane too since nothing is left.
            res_content += (
                '<div id="triage-doc-pane" hx-swap-oob="innerHTML">'
                '<div class="flex items-center justify-center flex-1">'
                '<div class="text-center p-8">'
                '<span class="material-symbols-outlined text-4xl text-outline mb-3">check_circle</span>'
                '<h3 class="text-sm font-black text-on-surface uppercase tracking-widest">Queue Clear</h3>'
                "</div></div></div>"
            )
            res_content += global_oob
            response = HTMLResponse(res_content)
        else:
            bundle = next((b for b in bundles if b.key == bundle_key), None)
            if bundle:
                # Bundle still has documents — return the updated bundle group OOB.
                res_content = _render_bundle_group_oob(request, bundle, triage_service)
                res_content += global_oob
                response = HTMLResponse(res_content)
            else:
                # This bundle is now empty, but others remain — delete the group from DOM.
                res_content = f'<div id="triage-bundle-group-{bundle_key}" hx-swap-oob="delete"></div>'
                res_content += global_oob
                response = HTMLResponse(res_content)

        response.headers["HX-Trigger"] = json.dumps(trigger)
        return response

    return HTMLResponse("", status_code=200)


@router.get("/document/{doc_id}")
async def document_detail(request: Request, doc_id: int, db: Session = Depends(get_db)):
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

    context_type = request.query_params.get("context")

    if context_type == "triage":
        cases = (
            db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.title.asc()).all()
        )
        ctx = build_triage_hud_context(
            db, doc, cases=cases, OriginatorType=OriginatorType
        )
        return templates.TemplateResponse(request, "partials/hud/_container.html", ctx)

    if request.headers.get("hx-request"):
        ctx = build_hud_context(db, doc, mode="read")
        ctx["context"] = "embedded"
        ctx["case_id"] = doc.case_id
        ctx["first_child_id"] = ctx.get("first_child_id")
        ctx["bundle_prev_id"] = ctx.get("bundle_prev_id")
        ctx["bundle_next_id"] = ctx.get("bundle_next_id")
        return templates.TemplateResponse(request, "partials/hud/_container.html", ctx)

    # Full-page navigations redirect to the canonical full-screen HUD URL.
    if not doc.case_id or doc.case_id == "_TRIAGE":
        return RedirectResponse(url="/triage", status_code=302)
    return RedirectResponse(
        url=f"/cases/{doc.case_id}/document/{doc.id}", status_code=302
    )


# ---------------------------------------------------------------------------
# HUD reaction — unified endpoint used by all three HUD contexts (overlay /
# standalone / embedded). Triage's /triage/document/:id/reaction stays until
# Stage B when the triage pane migrates to the new embedded HUD.
# ---------------------------------------------------------------------------


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

    # OOB feed-card refresh for triage (selector misses gracefully outside triage)
    from app.api.triage import _render_doc_targeted_oob
    from app.services.triage_service import TriageService

    triage_service = TriageService(db)
    response.body += _render_doc_targeted_oob(request, doc, triage_service, db).encode()

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
async def get_pipeline_status(
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

    stages = doc.pipeline_stages or {}

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

    # Capture primitives — db session is request-scoped and must not be passed to a thread
    _doc_id = doc.id
    _batch_id = doc.ingest_batch_id
    threading.Thread(
        target=_dispatch_retry_task,
        args=(_doc_id, _batch_id, pipeline_stage),
        daemon=True,
    ).start()

    return templates.TemplateResponse(
        request,
        "partials/_pipeline_stepper.html",
        {"doc": doc},
    )


def _dispatch_retry_task(doc_id: int, batch_id, stage) -> None:
    from app.models.enums import PipelineStage

    if stage in (PipelineStage.EXTRACT, PipelineStage.METADATA):
        from app.tasks.document_processing import process_document_task

        process_document_task.delay(doc_id)

    elif stage == PipelineStage.BATCH_ANALYSIS:
        if batch_id:
            from app.tasks.analyze_batch import analyze_batch_task

            analyze_batch_task.delay(batch_id)

    elif stage == PipelineStage.ENRICH:
        from app.tasks.enrich_document import enrich_document_task

        enrich_document_task.delay(doc_id)

    elif stage == PipelineStage.RELATIONSHIPS:
        from app.tasks.detect_relationships import detect_relationships_task

        detect_relationships_task.delay(doc_id)

    elif stage == PipelineStage.CLAIMS:
        from app.tasks.extract_claims import extract_claims_task

        extract_claims_task.delay(doc_id)

    elif stage == PipelineStage.EMBEDDINGS:
        from app.tasks.generate_embedding import generate_embedding_task

        generate_embedding_task.delay(doc_id)


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
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Original file not found on disk")

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )
