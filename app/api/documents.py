import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db
from app.helpers import build_document_extraction_context, render_page
from app.models.database import Case, Document, DocumentRelationship, IngestStatus
from app.services.ingestion.service import (
    create_manual_upload_batch,
    ingest_file,
    process_uploaded_document,
)

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
                background_tasks.add_task(process_document_background, doc.id, db)
            except Exception as e:
                logger.warning(f"Background task failed for doc {doc.id}: {e}")

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
    from app.models.database import Case, Entity
    from app.models.enums import OriginatorType

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

    context = build_document_extraction_context(db, doc)
    context_type = request.query_params.get("context", "detail")

    if context_type == "triage":
        from app.models.enums import RelationshipConfidence, UserReactionType
        from app.services.triage_service import TriageService

        triage_service = TriageService(db)
        cases = db.query(Case).order_by(Case.title.asc()).all()
        entities = db.query(Entity).filter(Entity.source_document_id == doc.id).all()
        reactions = list(triage_service.get_reactions(doc.id))
        action_items = triage_service.get_action_items(doc.id)
        ai_relationships = (
            db.query(DocumentRelationship)
            .filter(
                DocumentRelationship.from_document_id == doc.id,
                DocumentRelationship.confidence == RelationshipConfidence.AI_DETECTED,
            )
            .options(
                joinedload(DocumentRelationship.to_document),
            )
            .all()
        )
        return templates.TemplateResponse(
            request,
            "partials/document_triage.html",
            {
                "doc": doc,
                "doc_id": doc.id,
                "cases": cases,
                "entities": entities,
                "context": context,
                "reactions": reactions,
                "action_items": action_items,
                "ai_relationships": ai_relationships,
                "OriginatorType": OriginatorType,
                "UserReactionType": UserReactionType,
                "RelationshipConfidence": RelationshipConfidence,
                "originator_colors": ORIGINATOR_COLORS,
                "originator_icons": ORIGINATOR_ICONS,
            },
        )

    if context_type == "activity":
        cases = db.query(Case).order_by(Case.title.asc()).all()
        all_cases = {c.id: c.title for c in cases}
        entities = db.query(Entity).filter(Entity.source_document_id == doc.id).all()

        return templates.TemplateResponse(
            request,
            "partials/document_activity.html",
            {
                "doc": doc,
                "doc_id": doc.id,
                "all_cases": all_cases,
                "entities": entities,
                "schedule_candidates": [],
                "linked_deadlines": [],
                "linked_hearings": [],
                "context": context,
                "OriginatorType": OriginatorType,
                "originator_colors": ORIGINATOR_COLORS,
                "originator_icons": ORIGINATOR_ICONS,
            },
        )
    else:
        entities = db.query(Entity).filter(Entity.source_document_id == doc.id).all()
        extraction_confidence = doc.extraction_confidence or {}
        return templates.TemplateResponse(
            request,
            "partials/document_detail.html",
            {
                "doc": doc,
                "doc_id": doc.id,
                "entities": entities,
                "extraction_confidence": extraction_confidence,
                "context": context,
                "originator_colors": ORIGINATOR_COLORS,
                "originator_icons": ORIGINATOR_ICONS,
            },
        )


def process_document_background(doc_id: int, db: Session):
    """Background task to process document after upload."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return

    doc.ingest_status = IngestStatus.PROCESSING
    doc.ingest_started_at = datetime.now(UTC)
    db.commit()

    try:
        process_uploaded_document(doc, db)
        doc.ingest_status = IngestStatus.COMPLETED
        db.commit()

    except Exception as e:
        doc.ingest_status = IngestStatus.FAILED
        doc.ingest_error = str(e)
        doc.ingest_completed_at = datetime.now(UTC)
        db.commit()
        return

    # Only run AI enrichment if ingestion succeeded
    import asyncio

    from app.services.ai_summary import _summarize_document_sync
    from app.services.embeddings import generate_embedding

    try:
        _summarize_document_sync(doc.id, db)
        asyncio.run(generate_embedding(doc.id))
    except Exception as ai_err:
        logger.warning(f"AI enrichment failed for doc {doc.id}: {ai_err}")

    doc.ingest_completed_at = datetime.now(UTC)
    db.commit()
