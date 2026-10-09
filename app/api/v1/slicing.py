"""Slicing review for scanned batches awaiting a split decision."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.core.paths import resolve_storage_path
from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.dependencies import get_current_user, get_db
from app.models.database import IngestBatch, User
from app.models.enums import IngestBatchStatus
from app.schemas.slicing import (
    ProposedCut,
    SlicingConfirm,
    SlicingConfirmed,
    SlicingPage,
    SlicingView,
)
from app.services.slicing_service import SlicingFailed, confirm_slices

router = APIRouter(prefix="/slicing", tags=["slicing"])


def owned_batch(
    batch_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> IngestBatch:
    batch = db.get(IngestBatch, batch_id)
    if batch is None or batch.owner_id != user.id:
        raise ApiError(404, "not_found", "Batch not found.")
    return batch


def _thumb_path(batch: IngestBatch, page: int) -> Path | None:
    from app.config import DATA_DIR

    root = str(DATA_DIR.resolve())
    candidates: list[Path] = []
    if batch.raw_source_path:
        candidates.append(
            resolve_storage_path(batch.raw_source_path).parent
            / "thumbs"
            / f"page_{page}.png"
        )
    pages = (batch.meta or {}).get("slicing", {}).get("pages", [])
    if 0 < page <= len(pages) and pages[page - 1].get("thumbnail_path"):
        candidates.append(Path(pages[page - 1]["thumbnail_path"]))
    import os

    for candidate in candidates:
        normalized = os.path.normpath(str(candidate))
        if normalized.startswith(root + os.sep) and os.path.isfile(normalized):
            return Path(normalized)
    return None


def _view(batch: IngestBatch) -> SlicingView:
    meta = (batch.meta or {}).get("slicing", {})
    pages = meta.get("pages", [])
    page_count = meta.get("page_count", len(pages)) or 0
    status = meta.get("status", "preparing")
    if batch.status != IngestBatchStatus.AWAITING_SLICING:
        status = "done"
    return SlicingView(
        batch_id=batch.id,
        subject=batch.subject,
        status=status
        if status in ("preparing", "ready", "failed", "done")
        else "preparing",
        page_count=page_count,
        pages=[
            SlicingPage(
                page=i + 1,
                text_head=(p.get("text_head") or "")[:400],
                text_tail=(p.get("text_tail") or "")[:400],
                has_thumbnail=_thumb_path(batch, i + 1) is not None,
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
        doc_ids = confirm_slices(db, locked, body.cuts)
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
    """Re-run the slice proposal for a batch whose preparation failed."""
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
