"""Multipart document upload into a case or the triage inbox."""

from __future__ import annotations

import logging
import os
from typing import Literal, cast

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.access_guards import check_owned_or_case_access
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import Case, Document, IngestBatch, User
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.schemas.upload import UploadResponse, UploadResult, UploadTarget
from app.services import access_service
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.converters import MAX_FILE_SIZE
from app.services.ingestion.service import create_manual_upload_batch, ingest_file
from app.tasks.document_processing import process_document_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["upload"])


def _editable_case(db: Session, user: User, case_id: str | None) -> Case | None:
    if not case_id or case_id == "_TRIAGE":
        return None
    case = db.get(Case, case_id)
    if case is None or not access_service.can_edit_case(db, user, case):
        raise ApiError(404, "not_found", "Case not found.")
    return case


def _editable_parent(db: Session, user: User, parent_id: int | None) -> None:
    """404 unless the user may edit the document the upload will be attached to."""
    if parent_id is None:
        return
    parent = db.get(Document, parent_id)
    if parent is None or not check_owned_or_case_access(
        db, user, owner_id=parent.owner_id, case_id=parent.case_id, edit=True
    ):
        raise ApiError(404, "not_found", "Parent document not found.")


@router.get("/target", response_model=UploadTarget)
def upload_target(
    case_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """What the upload modal needs when opened for a case: title and parent docs."""
    case = _editable_case(db, user, case_id)
    parents = (
        db.query(Document.id, Document.title)
        .filter(Document.case_id == case.id, Document.parent_id.is_(None))
        .order_by(Document.ingest_date.desc())
        .all()
        if case
        else []
    )
    return UploadTarget(
        case_id=case.id if case else None,
        case_title=case.title if case else None,
        parent_options=[{"id": pid, "title": title} for pid, title in parents],
    )


@router.post("", response_model=UploadResponse)
@limiter.limit("60/minute")
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    case_id: str | None = Form(None),
    parent_id: int | None = Form(None),
    split_scans: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Ingest files: non-.eml files share one manual batch; each .eml is its own batch.

    With ``split_scans`` each PDF is its own scan batch: multi-page PDFs go to
    slicing review, single-page ones straight to the pipeline. Triage only.
    """
    from app.services.ingestion.scan_folder import ingest_uploaded_scan
    from app.tasks.dispatch import dispatch_task

    case = _editable_case(db, user, case_id)
    _editable_parent(db, user, parent_id)
    if split_scans and (case is not None or parent_id is not None):
        raise ApiError(
            422,
            "split_needs_triage",
            "Splitting scans is only available when uploading to triage.",
        )
    target_case_id = case.id if case else None
    valid = [f for f in files if f.filename]
    if not valid:
        raise ApiError(422, "no_files", "No files selected.")

    def _ext(f: UploadFile) -> str:
        return os.path.splitext(cast(str, f.filename))[1].lower()

    def _is_scan(f: UploadFile) -> bool:
        return split_scans and _ext(f) == ".pdf"

    non_eml = [f for f in valid if _ext(f) != ".eml" and not _is_scan(f)]
    batch_id: int | None = None
    if non_eml:
        batch_id = create_manual_upload_batch(
            db,
            filenames=[cast(str, f.filename) for f in non_eml],
            case_id=target_case_id,
            owner_id=user.id,
        )
        db.commit()

    results: list[UploadResult] = []
    for file in valid:
        name = cast(str, file.filename)
        if _is_scan(file):
            try:
                scan = await ingest_uploaded_scan(db, file, user.id)
            except ValueError as exc:
                results.append(
                    UploadResult(filename=name, status="error", message=str(exc))
                )
            except Exception as exc:
                logger.error("Scan upload failed for %s: %s", name, exc, exc_info=True)
                results.append(
                    UploadResult(filename=name, status="error", message="Upload failed")
                )
            else:
                if scan is None:
                    results.append(
                        UploadResult(
                            filename=name,
                            status="duplicate",
                            message="Already ingested",
                        )
                    )
                else:
                    doc_id = (
                        db.query(Document.id)
                        .filter(Document.ingest_batch_id == scan.id)
                        .scalar()
                    )
                    results.append(
                        UploadResult(
                            filename=name,
                            status="queued",
                            doc_id=doc_id,
                            batch_id=scan.id,
                            slicing=scan.status == IngestBatchStatus.AWAITING_SLICING,
                        )
                    )
            continue
        if _ext(file) == ".eml":
            try:
                chunks: list[bytes] = []
                total = 0
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE_SIZE:
                        raise ValueError(
                            f"File too large. Maximum size: {MAX_FILE_SIZE // (1024 * 1024)}MB"
                        )
                    chunks.append(chunk)
                batch = ingest_raw_email(
                    db,
                    b"".join(chunks),
                    source_type=IngestBatchSourceType.MANUAL,
                    owner_id=user.id,
                )
                if batch:
                    results.append(
                        UploadResult(filename=name, status="queued", batch_id=batch.id)
                    )
                else:
                    results.append(
                        UploadResult(
                            filename=name,
                            status="duplicate",
                            message="Already ingested",
                        )
                    )
            except Exception as exc:
                logger.error("EML ingest failed for %s: %s", name, exc, exc_info=True)
                results.append(
                    UploadResult(filename=name, status="error", message="Upload failed")
                )
            continue
        try:
            doc = await ingest_file(
                file,
                target_case_id,
                db,
                parent_id,
                ingest_batch_id=batch_id,
                owner_id=user.id,
            )
            dispatch_task(process_document_task, doc.id)
            results.append(
                UploadResult(
                    filename=name, status="queued", doc_id=doc.id, batch_id=batch_id
                )
            )
        except HTTPException as exc:
            status: Literal["duplicate", "error"] = (
                "duplicate" if exc.status_code == 409 else "error"
            )
            results.append(
                UploadResult(filename=name, status=status, message=str(exc.detail))
            )
        except Exception as exc:
            logger.error("Upload failed for %s: %s", name, exc, exc_info=True)
            results.append(
                UploadResult(filename=name, status="error", message="Upload failed")
            )

    if batch_id is not None:
        remaining = (
            db.query(Document.id).filter(Document.ingest_batch_id == batch_id).count()
        )
        if remaining == 0:
            db.query(IngestBatch).filter(IngestBatch.id == batch_id).delete(
                synchronize_session=False
            )
            db.commit()
            batch_id = None

    queued = sum(1 for r in results if r.status == "queued")
    return UploadResponse(
        results=results, queued=queued, failed=len(results) - queued, batch_id=batch_id
    )
