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


def _archive_batch(processing_batch_dir: Path, batch_id: str) -> None:
    from datetime import UTC, datetime

    dest = SCAN_PROCESSED_DIR / datetime.now(tz=UTC).date().isoformat() / batch_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(processing_batch_dir), str(dest))


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


def scan_and_ingest(db: Session) -> int:
    """Pick up all ready files from incoming/, ingest each. Returns count processed."""
    try:
        candidates = sorted(SCAN_INCOMING_DIR.iterdir())
    except OSError:
        return 0

    processed = 0
    for incoming_path in candidates:
        if incoming_path.name.startswith("."):
            continue
        if incoming_path.suffix.lower() in _IGNORE_SUFFIXES:
            continue
        if not incoming_path.is_file():
            continue
        if not _is_ready(incoming_path):
            continue

        batch_id = str(uuid4())
        processing_batch_dir = SCAN_PROCESSING_DIR / batch_id
        processing_batch_dir.mkdir(parents=True, exist_ok=True)
        dest_path = processing_batch_dir / f"original{incoming_path.suffix.lower()}"

        # Atomic claim — POSIX rename; another worker hitting the same file gets FileNotFoundError
        try:
            shutil.move(str(incoming_path), str(dest_path))
        except FileNotFoundError:
            shutil.rmtree(processing_batch_dir, ignore_errors=True)
            continue

        # Reject non-PDF files after claiming (prevents other workers from touching them)
        if dest_path.suffix.lower() != ".pdf":
            _fail_batch(
                processing_batch_dir,
                batch_id,
                f"Unsupported file type '{incoming_path.suffix}' — only .pdf is accepted in the ingest folder.",
            )
            continue

        # Compute SHA-256 of the file for dedup
        try:
            file_bytes = dest_path.read_bytes()
        except OSError as exc:
            _fail_batch(processing_batch_dir, batch_id, f"Could not read file: {exc}")
            continue

        source_hash = hashlib.sha256(file_bytes).hexdigest()

        try:
            batch = ingest_scanned_file(db, dest_path, batch_id, source_hash)
            if batch is None:
                # Duplicate — silently skip; clean up processing dir
                shutil.rmtree(processing_batch_dir, ignore_errors=True)
                logger.info(
                    "scan_and_ingest: duplicate file skipped (hash=%s)", source_hash
                )
            else:
                _archive_batch(processing_batch_dir, batch_id)
                processed += 1
        except Exception as exc:
            logger.error(
                "scan_and_ingest: ingest failed for %s: %s", incoming_path.name, exc
            )
            _fail_batch(processing_batch_dir, batch_id, str(exc))

    return processed
