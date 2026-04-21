import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Case, Document
from app.models.enums import OriginatorType, UserReactionType
from app.repositories.document_pin import DocumentPinRepository
from app.repositories.user_reaction import UserReactionRepository
from app.services.case_dashboard_service import summary_bullets_from_ai_summary
from app.services.hud_context import build_hud_context, build_triage_hud_context
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
            .filter(Document.case_id == case_id, Document.parent_id is None)
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
    background_tasks: BackgroundTasks,
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
    ingest_batch_id = None
    if valid_files:
        ingest_batch_id = create_manual_upload_batch(
            db,
            filenames=[f.filename for f in valid_files],
            case_id=case_id,
        )
        db.commit()

    for file in files:
        if not file.filename:
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

            try:
                process_document_task.delay(doc.id)
            except Exception as e:
                logger.warning(f"Celery task dispatch failed for doc {doc.id}: {e}")

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

    # Return a refresh trigger or simple success message
    return HTMLResponse(
        '<div hx-trigger="load" hx-get="/activity" hx-target="body"></div>',
        status_code=200,
    )


@router.delete("/document/{doc_id}")
async def delete_document(doc_id: int, db: Session = Depends(get_db)):
    """Delete a document and its associated file."""
    from app.services.document_service import DocumentService

    doc_service = DocumentService(db)
    if doc_service.delete_document(doc_id):
        return HTMLResponse("", status_code=200)
    raise HTTPException(status_code=404, detail="Document not found")


@router.get("/document/{doc_id}/activity-item")
async def document_activity_item(
    request: Request, doc_id: int, db: Session = Depends(get_db)
):
    """Return a single document's activity feed item for polling/updates."""
    from app.models.database import Case

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return HTMLResponse("", status_code=404)

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    return templates.TemplateResponse(
        request,
        "partials/activity_feed_items.html",
        {
            "documents": [doc],
            "case_titles": case_titles,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
        },
    )


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

    # HTMX callers (document_card, timeline_item, review_card) get the embedded
    # read-mode HUD as a self-contained fragment into their preview panes.
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


# ---------------------------------------------------------------------------
# HUD summary approve/reject — separate from triage's approve-summary so
# both paths can coexist until Stage B.
# ---------------------------------------------------------------------------


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
        doc.ai_summary_status = "approved"
        doc.ai_summary_approved_at = datetime.now()
    elif action == "reject":
        doc.ai_summary_status = "pending"
        doc.ai_summary = None
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
