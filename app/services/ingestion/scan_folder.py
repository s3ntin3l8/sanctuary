"""Scan-folder ingest driver — polls DATA_DIR/scans/incoming/ every N seconds."""

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.config import (
    SCAN_FAILED_DIR,
    SCAN_INCOMING_DIR,
    SCAN_PROCESSED_DIR,
    SCAN_PROCESSING_DIR,
    SCAN_PROCESSING_STALE_SECONDS,
)
from app.core.paths import to_storage_path
from app.models.database import IngestBatch
from app.services.ingestion.batch_orchestrator import ingest_scanned_file
from app.services.ingestion.converters import MAX_FILE_SIZE, validate_file_magic

logger = logging.getLogger(__name__)

_IGNORE_SUFFIXES = {".part", ".tmp", ".crdownload"}
# Written next to the claimed file so the owner survives the move into
# processed/ — reconcile_processed_orphans needs it if the ingest never committed.
_OWNER_SIDECAR = ".owner"
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

    # Bound memory before touching the content: the file is hashed in chunks
    # (never held whole) and rendered page by page later, but an unbounded drop
    # into the folder would still cost unbounded time and disk. Same cap as
    # uploads.
    try:
        size = dest_path.stat().st_size
    except OSError as exc:
        _fail_batch(processing_batch_dir, batch_id, f"Could not read file: {exc}")
        return 0
    if size > MAX_FILE_SIZE:
        _fail_batch(
            processing_batch_dir,
            batch_id,
            f"File is {size / (1024 * 1024):.1f} MB; the ingest folder accepts at "
            f"most {MAX_FILE_SIZE / (1024 * 1024):g} MB. Split the scan and try again.",
        )
        return 0

    try:
        source_hash = _sha256_file(dest_path)
    except OSError as exc:
        _fail_batch(processing_batch_dir, batch_id, f"Could not read file: {exc}")
        return 0

    if owner_id is not None:
        try:
            (processing_batch_dir / _OWNER_SIDECAR).write_text(str(owner_id))
        except OSError:
            pass  # only needed for crash recovery; the ingest itself doesn't use it

    archive_dir = None
    try:
        archive_dir = _archive_batch(processing_batch_dir, batch_id)
    except Exception as exc:
        logger.error(
            "scan_and_ingest: could not archive %s: %s", incoming_path.name, exc
        )
        _fail_batch(processing_batch_dir, batch_id, str(exc))
        return 0
    return _ingest_archived(
        db, archive_dir, dest_path.name, batch_id, source_hash, owner_id
    )


def _ingest_archived(
    db: Session,
    archive_dir: Path,
    pdf_name: str,
    batch_id: str,
    source_hash: str,
    owner_id: int | None,
) -> int:
    """Create the batch for a PDF that already sits in processed/<date>/<batch_id>/.

    Returns 1 for a new batch, 0 for a duplicate or a failure (moved to failed/).
    """
    try:
        batch = ingest_scanned_file(
            db, archive_dir / pdf_name, batch_id, source_hash, owner_id=owner_id
        )
        if batch is None:
            shutil.rmtree(archive_dir, ignore_errors=True)
            logger.info(
                "scan_and_ingest: duplicate file skipped (hash=%s)", source_hash
            )
            return 0
        return 1
    except Exception as exc:
        # ingest_scanned_file shares `db` across every file in this tick's
        # scan_and_ingest loop — a DB-level failure (not just an application
        # exception) leaves the session in a failed-transaction state where
        # every subsequent query raises PendingRollbackError. Without this,
        # one bad file poisons the session for the rest of the tick: every
        # later file in the same incoming/ listing gets wrongly moved to
        # failed/ with a misleading error, even though nothing was actually
        # wrong with them.
        db.rollback()
        logger.error("scan_and_ingest: ingest failed for %s: %s", pdf_name, exc)
        _fail_batch(archive_dir, batch_id, str(exc))
        return 0


async def ingest_uploaded_scan(
    db: Session, file: UploadFile, owner_id: int | None
) -> IngestBatch | None:
    """Ingest an uploaded PDF through the scan pipeline (slicing for multi-page).

    Same on-disk layout as a folder scan (processed/<date>/<uuid>/original.pdf
    plus owner sidecar), so dedup, slicing prep, recovery and bundle deletion
    treat it identically. Returns None for a duplicate; raises ``ValueError``
    for an invalid or oversized file.
    """
    import aiofiles

    batch_id = str(uuid4())
    processing_dir = SCAN_PROCESSING_DIR / batch_id
    processing_dir.mkdir(parents=True, exist_ok=True)
    dest = processing_dir / "original.pdf"
    digest = hashlib.sha256()
    try:
        total = 0
        async with aiofiles.open(dest, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    raise ValueError(
                        f"File too large. Maximum size: {MAX_FILE_SIZE // (1024 * 1024)}MB"
                    )
                digest.update(chunk)
                await out.write(chunk)
        if validate_file_magic(str(dest)) != ".pdf":
            raise ValueError("File content is not a PDF.")
        if owner_id is not None:
            (processing_dir / _OWNER_SIDECAR).write_text(str(owner_id))
    except Exception:
        shutil.rmtree(processing_dir, ignore_errors=True)
        raise

    archive_dir = _archive_batch(processing_dir, batch_id)
    try:
        batch = ingest_scanned_file(
            db,
            archive_dir / dest.name,
            batch_id,
            digest.hexdigest(),
            owner_id=owner_id,
            display_name=os.path.basename(file.filename or "") or None,
        )
    except Exception as exc:
        db.rollback()
        _fail_batch(archive_dir, batch_id, str(exc))
        raise
    if batch is None:
        shutil.rmtree(archive_dir, ignore_errors=True)
    return batch


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


_RECONCILE_LOOKBACK_DAYS = 7
_RECONCILE_INTERVAL_SECONDS = 300
_last_reconcile_at = 0.0


def reconcile_processed_orphans(
    db: Session,
    *,
    min_age_seconds: int = SCAN_PROCESSING_STALE_SECONDS,
    default_owner_id: int | None = None,
    lookback_days: int = _RECONCILE_LOOKBACK_DAYS,
) -> int:
    """Re-ingest PDFs archived to processed/ whose batch row was never committed.

    ``_ingest_one`` archives the file *before* ``ingest_scanned_file`` commits
    the IngestBatch. A crash in that narrow window leaves a file in processed/
    that looks successfully processed but has no row, so it is invisible
    everywhere (#155). The window is a few DB operations wide, so anything older
    than ``min_age_seconds`` with no IngestBatch pointing at it was genuinely
    lost (deleting a bundle removes its original, so a deliberately deleted file
    is not resurrected). It is ingested again from where it sits — the original
    owner comes from the ``.owner`` sidecar, else ``default_owner_id`` — and the
    owner-scoped source-hash dedup makes a repeat harmless.

    Only the last ``lookback_days`` date folders are examined, with one batched
    query for all candidates, so the cost per call stays flat as processed/
    grows over months.

    Returns the number of batches created.
    """
    from datetime import UTC, datetime, timedelta

    from app.models.database import IngestBatch

    try:
        date_dirs = sorted(p for p in SCAN_PROCESSED_DIR.iterdir() if p.is_dir())
    except OSError:
        return 0
    oldest = (datetime.now(tz=UTC) - timedelta(days=lookback_days)).date().isoformat()
    date_dirs = [d for d in date_dirs if d.name >= oldest]

    now = time.time()
    candidates: list[tuple[Path, Path]] = []  # (batch_dir, pdf)
    for date_dir in date_dirs:
        try:
            batch_dirs = sorted(p for p in date_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for batch_dir in batch_dirs:
            pdf = batch_dir / "original.pdf"
            try:
                if (
                    not pdf.is_file()
                    or now - batch_dir.stat().st_mtime < min_age_seconds
                ):
                    continue
            except OSError:
                continue
            candidates.append((batch_dir, pdf))
    if not candidates:
        return 0

    stored = {to_storage_path(pdf): (batch_dir, pdf) for batch_dir, pdf in candidates}
    known = {
        row[0]
        for row in db.query(IngestBatch.raw_source_path).filter(
            IngestBatch.raw_source_path.in_(list(stored))
        )
    }

    recovered = 0
    for key, (batch_dir, pdf) in stored.items():
        if key in known:
            continue
        owner_id = default_owner_id
        try:
            owner_id = int((batch_dir / _OWNER_SIDECAR).read_text().strip())
        except (OSError, ValueError):
            pass
        logger.warning(
            "scan_and_ingest: %s was archived but has no batch row (a crash "
            "between the archive move and the DB commit) — ingesting it again",
            pdf,
        )
        recovered += _ingest_archived(
            db, batch_dir, pdf.name, batch_dir.name, _sha256_file(pdf), owner_id
        )
    return recovered


def sweep_stale_processing_dirs(
    *, stale_after_seconds: int = SCAN_PROCESSING_STALE_SECONDS
) -> int:
    """Move processing/ subdirectories abandoned by a crashed worker to failed/.

    A file is claimed into processing/<batch_id>/ via an atomic rename in
    _ingest_one, then either archived to processed/ or moved to failed/
    within the same call — normally a few seconds, never more than a single
    Docling-free ingest_scanned_file call. A directory still there past
    stale_after_seconds was claimed by a worker that crashed (or was killed)
    before reaching either terminal move, and would otherwise sit invisible
    in processing/ forever — neither retried, failed, nor visible in the
    triage/failed UI.
    """
    try:
        candidates = sorted(SCAN_PROCESSING_DIR.iterdir())
    except OSError:
        return 0

    now = time.time()
    swept = 0
    for entry in candidates:
        if not entry.is_dir():
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < stale_after_seconds:
            continue
        batch_id = entry.name
        logger.warning(
            "scan_and_ingest: sweeping stale processing/ dir %s (age=%.0fs) — "
            "likely abandoned by a crashed worker; move it back to incoming/ "
            "to retry",
            batch_id,
            age,
        )
        _fail_batch(
            entry,
            batch_id,
            f"Abandoned in processing/ for {age:.0f}s — likely a crashed "
            "worker. Move the original file back to incoming/ to retry.",
        )
        swept += 1
    return swept


def scan_and_ingest(db: Session) -> int:
    """Pick up ready files from incoming/ and ingest each, attributing ownership.

    Files inside a per-user subfolder ``incoming/<username>/`` are owned by that
    user; files dropped directly in the root ``incoming/`` are attributed to the
    bootstrap admin (who can reassign the resulting case later).
    """
    sweep_stale_processing_dirs()
    from app.models.database import User
    from app.services import auth_service

    try:
        candidates = sorted(SCAN_INCOMING_DIR.iterdir())
    except OSError:
        return 0

    username_to_id = {u.username: u.id for u in db.query(User).all() if u.username}
    admin = auth_service.get_or_create_bootstrap_admin(db)
    if admin is None:
        return 0  # no admin configured yet — nothing owns ingested scans
    admin_id = admin.id

    global _last_reconcile_at
    if time.time() - _last_reconcile_at >= _RECONCILE_INTERVAL_SECONDS:
        _last_reconcile_at = time.time()
        # Its own session: a failure while re-ingesting an orphan rolls back,
        # and that must not touch this tick's session or its incoming/ work.
        from app.config import SessionLocal

        with SessionLocal() as reconcile_db:
            reconcile_processed_orphans(reconcile_db, default_owner_id=admin_id)

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
