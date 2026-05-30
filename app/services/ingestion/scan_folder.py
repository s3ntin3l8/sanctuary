"""Scan-folder ingest driver — polls DATA_DIR/scans/incoming/ every N seconds."""

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import (
    SCAN_FAILED_DIR,
    SCAN_INCOMING_DIR,
    SCAN_PROCESSED_DIR,
    SCAN_PROCESSING_DIR,
)
from app.services.ingestion.batch_orchestrator import ingest_scanned_file

logger = logging.getLogger(__name__)

_IGNORE_SUFFIXES = {".part", ".tmp", ".crdownload"}
_MTIME_GUARD_SECONDS = int(os.getenv("SCAN_MTIME_GUARD_SECONDS", "5"))


def _is_ready(path: Path) -> bool:
    """Skip files that are still being written (mtime too recent)."""
    try:
        return time.time() - path.stat().st_mtime >= _MTIME_GUARD_SECONDS
    except OSError:
        return False


def _archive_batch(processing_batch_dir: Path, batch_id: str) -> Path:
    from datetime import UTC, datetime

    dest = SCAN_PROCESSED_DIR / datetime.now(tz=UTC).date().isoformat() / batch_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(processing_batch_dir), str(dest))
    return dest


def _fail_batch(processing_batch_dir: Path, batch_id: str, reason: str) -> None:
    dest = SCAN_FAILED_DIR / batch_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(processing_batch_dir), str(dest))
    except Exception:
        dest = SCAN_FAILED_DIR / batch_id
        dest.mkdir(parents=True, exist_ok=True)
    error_log = dest / "error.log"
    try:
        error_log.write_text(reason)
    except Exception:
        pass


def _ingest_one(db: Session, incoming_path: Path, owner_id: int | None) -> int:
    """Claim and ingest a single incoming file as `owner_id`. Returns 1 if a new
    batch was created, 0 if skipped/duplicate."""
    if incoming_path.name.startswith("."):
        return 0
    if incoming_path.suffix.lower() in _IGNORE_SUFFIXES:
        return 0
    if not incoming_path.is_file():
        return 0
    if not _is_ready(incoming_path):
        return 0

    batch_id = str(uuid4())
    processing_batch_dir = SCAN_PROCESSING_DIR / batch_id
    processing_batch_dir.mkdir(parents=True, exist_ok=True)
    dest_path = processing_batch_dir / f"original{incoming_path.suffix.lower()}"

    # Atomic claim — POSIX rename; another worker hitting the same file gets FileNotFoundError
    try:
        shutil.move(str(incoming_path), str(dest_path))
    except FileNotFoundError:
        shutil.rmtree(processing_batch_dir, ignore_errors=True)
        return 0

    # Reject non-PDF files after claiming (prevents other workers from touching them)
    if dest_path.suffix.lower() != ".pdf":
        _fail_batch(
            processing_batch_dir,
            batch_id,
            f"Unsupported file type '{incoming_path.suffix}' — only .pdf is accepted in the ingest folder.",
        )
        return 0

    try:
        file_bytes = dest_path.read_bytes()
    except OSError as exc:
        _fail_batch(processing_batch_dir, batch_id, f"Could not read file: {exc}")
        return 0

    source_hash = hashlib.sha256(file_bytes).hexdigest()

    archive_dir = None
    try:
        archive_dir = _archive_batch(processing_batch_dir, batch_id)
        archived_pdf_path = archive_dir / dest_path.name
        batch = ingest_scanned_file(
            db, archived_pdf_path, batch_id, source_hash, owner_id=owner_id
        )
        if batch is None:
            shutil.rmtree(archive_dir, ignore_errors=True)
            logger.info(
                "scan_and_ingest: duplicate file skipped (hash=%s)", source_hash
            )
            return 0
        return 1
    except Exception as exc:
        logger.error(
            "scan_and_ingest: ingest failed for %s: %s", incoming_path.name, exc
        )
        failed_source = archive_dir or processing_batch_dir
        _fail_batch(failed_source, batch_id, str(exc))
        return 0


def scan_and_ingest(db: Session) -> int:
    """Pick up ready files from incoming/ and ingest each, attributing ownership.

    Files inside a per-user subfolder ``incoming/<username>/`` are owned by that
    user; files dropped directly in the root ``incoming/`` are attributed to the
    bootstrap admin (who can reassign the resulting case later).
    """
    from app.models.database import User
    from app.services import auth_service

    try:
        candidates = sorted(SCAN_INCOMING_DIR.iterdir())
    except OSError:
        return 0

    username_to_id = {u.username: u.id for u in db.query(User).all() if u.username}
    admin_id = auth_service.get_or_create_bootstrap_admin(db).id

    processed = 0
    for entry in candidates:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            owner_id = username_to_id.get(entry.name, admin_id)
            try:
                sub_files = sorted(entry.iterdir())
            except OSError:
                continue
            for f in sub_files:
                processed += _ingest_one(db, f, owner_id)
        elif entry.is_file():
            processed += _ingest_one(db, entry, admin_id)

    return processed
