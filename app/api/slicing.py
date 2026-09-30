"""3c — Slicing review routes."""

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.paths import resolve_storage_path, to_storage_path
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.helpers import render_page
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchStatus

router = APIRouter(prefix="/ingest/slice", tags=["slicing"])


def _get_batch(batch_id: int, db: Session) -> IngestBatch:
    batch = db.get(IngestBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@router.get("/{batch_id}", response_class=HTMLResponse)
async def slicing_review(
    request: Request, batch_id: int, db: Session = Depends(get_db)
):
    batch = _get_batch(batch_id, db)
    slicing_meta = (batch.meta or {}).get("slicing", {"status": "preparing"})
    return render_page(
        request,
        "pages/slicing_review.html",
        db=db,
        batch=batch,
        slicing_data=slicing_meta,
    )


@router.get("/{batch_id}/status")
async def slicing_status(batch_id: int, db: Session = Depends(get_db)):
    """Poll endpoint for the loading state — returns the slicing sub-dict."""
    batch = _get_batch(batch_id, db)
    slicing_meta = (batch.meta or {}).get("slicing", {"status": "preparing"})
    return JSONResponse(slicing_meta)


@router.get("/{batch_id}/thumb/{page}")
async def slicing_thumb(batch_id: int, page: int, db: Session = Depends(get_db)):
    batch = _get_batch(batch_id, db)
    slicing_meta = (batch.meta or {}).get("slicing", {})
    pages = slicing_meta.get("pages", [])
    page_count = slicing_meta.get("page_count", len(pages))

    if page < 1 or page > page_count:
        raise HTTPException(status_code=400, detail=f"Page {page} out of range")

    from app.config import DATA_DIR

    data_root = DATA_DIR.resolve()

    def _serve_if_under_data_dir(p: Path):
        resolved = p.resolve()
        if not str(resolved).startswith(str(data_root) + "/") and resolved != data_root:
            return None
        if not resolved.exists():
            return None
        return FileResponse(str(resolved), media_type="image/png")

    if batch.raw_source_path:
        candidate = (
            resolve_storage_path(batch.raw_source_path).parent
            / "thumbs"
            / f"page_{page}.png"
        )
        served = _serve_if_under_data_dir(candidate)
        if served is not None:
            return served

    if pages and (page - 1) < len(pages):
        stored_path = pages[page - 1].get("thumbnail_path")
        if stored_path:
            served = _serve_if_under_data_dir(Path(stored_path))
            if served is not None:
                return served

    raise HTTPException(status_code=404, detail=f"Thumbnail for page {page} not found")


@router.post("/{batch_id}/confirm")
def slicing_confirm(
    request: Request,
    batch_id: int,
    cuts: str = Form(...),
    db: Session = Depends(get_db),
):
    # No `await` in this body — a sync def lets FastAPI run the blocking
    # pdfium split/save/hash work (and the DB calls below) in its threadpool
    # instead of blocking the event loop for the whole slicing operation.
    from app.services.ingestion.cover_letter_wiring import wire_cover_letter

    # Idempotency guard — a real row lock, not just a refresh. A second
    # concurrent confirm blocks here until the first commits or rolls back,
    # then sees the batch's final status instead of both requests racing
    # into duplicate slicing (double-submit, or two tabs open on the same
    # review page).
    batch = db.get(IngestBatch, batch_id, with_for_update=True)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    if batch.status != IngestBatchStatus.AWAITING_SLICING:
        return RedirectResponse("/triage", status_code=303)

    slicing_meta = (batch.meta or {}).get("slicing", {})
    page_count = slicing_meta.get("page_count", 0)
    if not page_count:
        raise HTTPException(status_code=400, detail="Batch slicing metadata missing")

    # Parse and validate cut positions against the actual page_count.
    # `cuts` must be a JSON array — json.loads("5") or json.loads("null")
    # succeed but yield a non-list (int/None), which then raises TypeError
    # inside the comprehension below rather than the ValueError/
    # JSONDecodeError this used to only catch, escaping to a bare 500.
    try:
        raw_cuts = json.loads(cuts)
        if not isinstance(raw_cuts, list):
            raise ValueError("cuts must be a JSON array")
        cut_positions = sorted({int(c) for c in raw_cuts if 1 <= int(c) < page_count})
    except (json.JSONDecodeError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid cuts JSON") from None

    if not batch.raw_source_path:
        raise HTTPException(status_code=409, detail="Source PDF no longer available")
    pdf_path = resolve_storage_path(batch.raw_source_path)
    if not pdf_path.exists():
        raise HTTPException(status_code=409, detail="Source PDF no longer available")

    # Compute slice ranges: [(start_page, end_page), ...]  1-indexed
    boundaries = [0] + cut_positions + [page_count]
    slices = [
        (boundaries[i] + 1, boundaries[i + 1]) for i in range(len(boundaries) - 1)
    ]

    import pypdfium2 as pdfium

    docs_to_process: list[Document] = []
    first_doc_id: int | None = None
    written_slice_paths: list[Path] = []

    try:
        src_pdf = pdfium.PdfDocument(str(pdf_path))

        for slice_idx, (start_page, end_page) in enumerate(slices):
            slice_pdf = pdfium.PdfDocument.new()
            page_indices = list(range(start_page - 1, end_page))
            slice_pdf.import_pages(src_pdf, page_indices)

            slice_filename = pdf_path.parent / f"slice_{slice_idx + 1}.pdf"
            slice_pdf.save(str(slice_filename))
            slice_pdf.close()
            written_slice_paths.append(slice_filename)

            slice_bytes = slice_filename.read_bytes()
            content_hash = hashlib.sha256(slice_bytes).hexdigest()

            slice_page_count = end_page - start_page + 1

            doc = Document(
                title=f"{pdf_path.stem} – Part {slice_idx + 1}",
                owner_id=batch.owner_id,  # sliced docs inherit the batch's owner
                file_path=to_storage_path(slice_filename),
                original_filename=slice_filename.name,
                content_hash=content_hash,
                case_id="_TRIAGE",
                ingest_batch_id=batch.id,
                meta={"slice_range": [start_page, end_page]},
                page_count=slice_page_count,
            )
            from app.services.pipeline_status import initialize as _pipeline_init

            db.add(doc)
            db.flush()
            _pipeline_init(doc, batched=True, db=db)
            docs_to_process.append(doc)

            if slice_idx == 0:
                first_doc_id = doc.id

        src_pdf.close()

        # Wire cover letter + enclosures
        if first_doc_id and len(docs_to_process) > 1:
            child_ids = [d.id for d in docs_to_process[1:]]
            wire_cover_letter(db, first_doc_id, child_ids, court_relay=True)

        batch.status = IngestBatchStatus.PROCESSING
        db.commit()

    except Exception as exc:
        db.rollback()
        # The DB rows for these slices are gone (rolled back), but the slice
        # PDFs already written to disk are not — remove them so a retry
        # doesn't inherit stale/orphaned files from this failed attempt.
        for path in written_slice_paths:
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        raise HTTPException(status_code=500, detail=f"Slicing failed: {exc}") from exc

    from app.tasks.dispatch import dispatch_task
    from app.tasks.document_processing import process_document_task

    for doc in docs_to_process:
        dispatch_task(process_document_task, doc.id)

    return RedirectResponse("/triage", status_code=303)


@router.post("/{batch_id}/retry")
@limiter.limit("10/minute")
async def slicing_retry(request: Request, batch_id: int, db: Session = Depends(get_db)):
    """Re-enqueue prepare_slicing_task for a failed batch."""
    batch = _get_batch(batch_id, db)
    if batch.status != IngestBatchStatus.AWAITING_SLICING:
        raise HTTPException(status_code=409, detail="Batch is not awaiting slicing")

    meta = dict(batch.meta or {})
    meta["slicing"] = {**meta.get("slicing", {}), "status": "preparing"}
    batch.meta = meta
    db.commit()

    from app.tasks.dispatch import dispatch_task
    from app.tasks.prepare_slicing import prepare_slicing_task

    dispatch_task(prepare_slicing_task, batch_id)
    return RedirectResponse(f"/ingest/slice/{batch_id}", status_code=303)
