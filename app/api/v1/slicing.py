"""Slicing review: scanned batches awaiting a split decision, and existing PDF documents."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.api.access_guards import require_document_access
from app.api.v1.errors import ApiError
from app.core.paths import resolve_storage_path
from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.dependencies import get_current_user, get_db
from app.models.database import Document, IngestBatch, User
from app.models.enums import IngestBatchStatus
from app.schemas.slicing import (
    ProposedCut,
    SlicingConfirm,
    SlicingConfirmed,
    SlicingPage,
    SlicingView,
)
from app.services.slicing_service import (
    SlicingFailed,
    SourceHasUserData,
    SourceInFlight,
    confirm_document_slices,
    confirm_slices,
)

router = APIRouter(prefix="/slicing", tags=["slicing"])


def owned_batch(
    batch_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> IngestBatch:
    batch = db.get(IngestBatch, batch_id)
    if batch is None or batch.owner_id != user.id:
        raise ApiError(404, "not_found", "Batch not found.")
    return batch


def _thumb_path(source: IngestBatch | Document, page: int) -> Path | None:
    from app.config import DATA_DIR

    root = str(DATA_DIR.resolve())
    candidates: list[Path] = []
    # A batch owns its folder; a document's thumbnails are only found by the
    # path recorded for each page.
    if isinstance(source, IngestBatch) and source.raw_source_path:
        candidates.append(
            resolve_storage_path(source.raw_source_path).parent
            / "thumbs"
            / f"page_{page}.png"
        )
    pages = (source.meta or {}).get("slicing", {}).get("pages", [])
    if 0 < page <= len(pages) and pages[page - 1].get("thumbnail_path"):
        candidates.append(Path(pages[page - 1]["thumbnail_path"]))
    import os

    for candidate in candidates:
        normalized = os.path.normpath(str(candidate))
        if normalized.startswith(root + os.sep) and os.path.isfile(normalized):
            return Path(normalized)
    return None


def _view(source: IngestBatch | Document) -> SlicingView:
    meta = (source.meta or {}).get("slicing", {})
    pages = meta.get("pages", [])
    page_count = meta.get("page_count", len(pages)) or 0
    status = meta.get("status", "preparing")
    progress = meta.get("progress") or {}
    if isinstance(source, IngestBatch):
        if source.status != IngestBatchStatus.AWAITING_SLICING:
            status = "done"
    elif not meta:
        status = "done"
    return SlicingView(
        batch_id=source.id if isinstance(source, IngestBatch) else None,
        document_id=source.id if isinstance(source, Document) else None,
        subject=(
            source.subject
            if isinstance(source, IngestBatch)
            else (source.original_filename or source.title)
        ),
        status=status
        if status in ("preparing", "ready", "failed", "done")
        else "preparing",
        page_count=page_count,
        pages=[
            SlicingPage(
                page=i + 1,
                text_head=(p.get("text_head") or "")[:400],
                text_tail=(p.get("text_tail") or "")[:400],
                has_thumbnail=_thumb_path(source, i + 1) is not None,
                blank=bool(p.get("blank")),
            )
            for i, p in enumerate(pages)
        ],
        proposed_cuts=[
            ProposedCut(
                page=c["page"],
                confidence=c.get("confidence"),
                # Persisted in batch.meta, so a proposal written before kinds existed has none.
                kind=c.get("kind", "attachment"),
                notes=c.get("notes"),
            )
            for c in meta.get("proposed_cuts", [])
            if isinstance(c, dict) and isinstance(c.get("page"), int)
        ],
        error=meta.get("error"),
        progress_done=progress.get("done"),
        progress_total=progress.get("total"),
        progress_phase=progress.get("phase"),
    )


@router.get("/{batch_id}", response_model=SlicingView)
def slicing(batch: IngestBatch = Depends(owned_batch)):
    return _view(batch)


@router.get("/{batch_id}/thumb/{page}", response_class=FileResponse)
def thumbnail(page: int, batch: IngestBatch = Depends(owned_batch)):
    path = _thumb_path(batch, page)
    if path is None:
        raise ApiError(404, "not_found", f"Thumbnail for page {page} not found.")
    return FileResponse(str(path), media_type="image/png")


@router.get("/{batch_id}/page/{page}", response_class=Response)
@limiter.limit("120/minute")
def page_image(request: Request, page: int, batch: IngestBatch = Depends(owned_batch)):
    """The full-size render of one page, for the viewer (rendered on demand)."""
    from app.services.ingestion.slicer import render_page_png

    if not batch.raw_source_path:
        raise ApiError(404, "not_found", "Batch has no source PDF.")
    pdf_path = resolve_storage_path(batch.raw_source_path)
    if not pdf_path.is_file():
        raise ApiError(404, "not_found", "Source PDF not found.")
    try:
        png = render_page_png(pdf_path, page)
    except pdfium.PdfiumError as exc:
        raise ApiError(404, "not_found", "Source PDF could not be read.") from exc
    if png is None:
        raise ApiError(404, "not_found", f"Page {page} not found.")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=600"},
    )


@router.post("/{batch_id}/confirm", response_model=SlicingConfirmed)
def confirm_slicing(
    body: SlicingConfirm,
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
):
    from app.tasks.dispatch import dispatch_task
    from app.tasks.document_processing import process_document_task

    locked = db.get(IngestBatch, batch.id, with_for_update=True)
    assert locked is not None
    # The identity map already holds this row from owned_batch; re-read it
    # under the lock so the status check sees the committed value.
    db.refresh(locked)
    try:
        doc_ids = confirm_slices(db, locked, body.cuts, body.discard)
    except SlicingFailed as exc:
        raise ApiError(500, "slicing_failed", str(exc)) from exc
    except ValueError as exc:
        code = "not_awaiting" if "not awaiting" in str(exc) else "unsliceable"
        raise ApiError(409, code, str(exc)) from exc
    for doc_id in doc_ids:
        dispatch_task(process_document_task, doc_id)
    return SlicingConfirmed(document_ids=doc_ids)


@router.post("/{batch_id}/retry", response_model=SlicingView)
@limiter.limit("10/minute")
def retry(
    request: Request,
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
):
    """Re-run the slice proposal (after a failure, or to redo a ready one)."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.prepare_slicing import prepare_slicing_task

    # Lock the row and re-read it under the lock (same as confirm_slicing), so
    # two concurrent retries serialize and only the first one dispatches.
    locked = db.get(IngestBatch, batch.id, with_for_update=True)
    assert locked is not None
    db.refresh(locked)
    if locked.status != IngestBatchStatus.AWAITING_SLICING:
        raise ApiError(409, "not_awaiting", "Batch is not awaiting slicing.")
    slicing = (locked.meta or {}).get("slicing", {})
    if slicing.get("status") == "preparing":
        raise ApiError(409, "already_preparing", "Slicing is already being prepared.")
    meta = dict(locked.meta or {})
    meta["slicing"] = {
        **{k: v for k, v in slicing.items() if k != "recovered"},
        "status": "preparing",
        "dispatched_at": now_utc().isoformat(),
    }
    locked.meta = meta
    db.commit()
    dispatch_task(prepare_slicing_task, locked.id)
    return _view(locked)


# --- Existing PDF documents (an email attachment is already a document) -------


def _sliceable_document(doc: Document) -> None:
    if not (doc.file_path or "").lower().endswith(".pdf") or (doc.page_count or 0) < 2:
        raise ApiError(
            409, "unsliceable", "Only a PDF with more than one page can be split."
        )


def _document_slicing(doc: Document) -> dict:
    slicing = (doc.meta or {}).get("slicing")
    if not slicing:
        raise ApiError(404, "not_found", "No split has been started for this document.")
    return slicing


def _is_stale(slicing: dict) -> bool:
    """A "preparing" mark whose task was lost: older than any task may run.

    Celery kills a task at CELERY_TASK_TIME_LIMIT, so a mark older than that has
    no live task behind it (the clock is the dispatch time this API stamped).
    """
    from datetime import datetime, timedelta

    from app.config import CELERY_TASK_TIME_LIMIT
    from app.core.timezone import ensure_utc

    try:
        stamped = ensure_utc(datetime.fromisoformat(slicing["dispatched_at"]))
    except (KeyError, ValueError, TypeError):
        return True
    return stamped is None or stamped < now_utc() - timedelta(
        seconds=CELERY_TASK_TIME_LIMIT
    )


def _begin_document_slicing(db: Session, doc: Document, *, redo: bool) -> None:
    """Mark the document as preparing and queue the proposal, unless one is
    already running (or ready and not asked to be redone)."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.prepare_slicing import prepare_document_slicing_task

    _sliceable_document(doc)
    locked = db.query(Document).filter(Document.id == doc.id).with_for_update().one()
    db.refresh(locked)
    slicing = dict((locked.meta or {}).get("slicing") or {})
    status = slicing.get("status")
    if status == "preparing" and not _is_stale(slicing):
        if redo:
            raise ApiError(
                409, "already_preparing", "Slicing is already being prepared."
            )
        return
    if status == "ready" and not redo:
        return
    meta = dict(locked.meta or {})
    meta["slicing"] = {
        "status": "preparing",
        "dispatched_at": now_utc().isoformat(),
    }
    locked.meta = meta
    db.commit()
    dispatch_task(prepare_document_slicing_task, locked.id)


@router.post("/documents/{doc_id}", response_model=SlicingView)
@limiter.limit("10/minute")
def start_document_slicing(
    request: Request,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Start (or resume) the split review of one multi-page PDF document."""
    _begin_document_slicing(db, doc, redo=False)
    db.refresh(doc)
    return _view(doc)


@router.get("/documents/{doc_id}", response_model=SlicingView)
def document_slicing(doc: Document = Depends(require_document_access())):
    _document_slicing(doc)
    return _view(doc)


@router.get("/documents/{doc_id}/thumb/{page}", response_class=FileResponse)
def document_thumbnail(page: int, doc: Document = Depends(require_document_access())):
    path = _thumb_path(doc, page)
    if path is None:
        raise ApiError(404, "not_found", f"Thumbnail for page {page} not found.")
    return FileResponse(str(path), media_type="image/png")


@router.get("/documents/{doc_id}/page/{page}", response_class=Response)
@limiter.limit("120/minute")
def document_page_image(
    request: Request, page: int, doc: Document = Depends(require_document_access())
):
    from app.services.ingestion.slicer import render_page_png

    if not doc.file_path:
        raise ApiError(404, "not_found", "Document has no PDF.")
    pdf_path = resolve_storage_path(doc.file_path)
    if not pdf_path.is_file():
        raise ApiError(404, "not_found", "Source PDF not found.")
    try:
        png = render_page_png(pdf_path, page)
    except pdfium.PdfiumError as exc:
        raise ApiError(404, "not_found", "Source PDF could not be read.") from exc
    if png is None:
        raise ApiError(404, "not_found", f"Page {page} not found.")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=600"},
    )


@router.post("/documents/{doc_id}/retry", response_model=SlicingView)
@limiter.limit("10/minute")
def retry_document_slicing(
    request: Request,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Compute a fresh proposal (after a failure, or to redo a ready one)."""
    _begin_document_slicing(db, doc, redo=True)
    db.refresh(doc)
    return _view(doc)


@router.post("/documents/{doc_id}/confirm", response_model=SlicingConfirmed)
def confirm_document_slicing(
    body: SlicingConfirm,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Replace the document by its parts. Refused when it carries your own work."""
    from app.services.triage_retry import rearm_batch_barriers
    from app.tasks.dispatch import dispatch_task
    from app.tasks.document_processing import process_document_task

    _document_slicing(doc)
    locked = db.query(Document).filter(Document.id == doc.id).with_for_update().one()
    db.refresh(locked)
    if (locked.meta or {}).get("slicing", {}).get("status") != "ready":
        raise ApiError(409, "not_ready", "The split proposal is not ready yet.")
    batch_id = locked.ingest_batch_id
    try:
        doc_ids = confirm_document_slices(db, locked, body.cuts, body.discard)
    except SourceInFlight as exc:
        raise ApiError(409, "in_flight", str(exc)) from exc
    except SourceHasUserData as exc:
        raise ApiError(409, "has_user_data", str(exc)) from exc
    except SlicingFailed as exc:
        raise ApiError(500, "slicing_failed", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(409, "unsliceable", str(exc)) from exc
    if batch_id is not None:
        # The batch ran its one-shot metadata/analysis barriers for the source;
        # the parts need them to fire again (and the siblings are re-enriched
        # with the parts' context by the analysis).
        rearm_batch_barriers(batch_id, db)
    for doc_id in doc_ids:
        dispatch_task(process_document_task, doc_id)
    return SlicingConfirmed(document_ids=doc_ids)
