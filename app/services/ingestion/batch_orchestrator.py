import hashlib
import logging
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pypdfium2 as pdfium
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.core.paths import to_storage_path
from app.core.timezone import now_utc
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.repositories.ingest_batch import (
    NO_NEW_DOCUMENTS,
    IngestBatchRepository,
    is_no_document_tombstone,
)
from app.services.ingestion.email_parser import parse_rfc822
from app.services.ingestion.extractors import (
    extract_az_court_from_subject,
    extract_internal_id_from_subject,
)
from app.tasks.dispatch import dispatch_task

logger = logging.getLogger(__name__)


def _resolve_owner_id(db: Session, owner_id: int | None) -> int | None:
    """Ingestion must never produce an unowned row. When the caller can't supply
    an owner (e.g. a legacy/root scan-folder file), fall back to the bootstrap
    admin so the document lands in *someone's* triage inbox."""
    if owner_id is not None:
        return owner_id
    try:
        from app.services import auth_service

        admin = auth_service.get_or_create_bootstrap_admin(db)
        return admin.id if admin is not None else None
    except Exception:  # pragma: no cover - defensive
        return None


def _sanitize_filename(name: str) -> str:
    """Sanitize filename while preserving Unicode characters (e.g. German umlauts)."""
    if not name:
        return "unnamed"
    name = unicodedata.normalize("NFC", name)
    safe_chars = "-_.() "
    result = "".join(c if c.isalnum() or c in safe_chars else "_" for c in name)
    return result.strip() or "unnamed"


def _try_assign_case_from_subject(
    db: Session, batch: IngestBatch, subject: str, owner_id: int | None
) -> None:
    """Set batch.case_id / batch.proceeding_id from the email subject line if possible.

    Tries internal_id (lawyer's file number, e.g. '8372/25') first — it maps 1:1 to
    Case.id per CLAUDE.md.  Falls back to az_court (court Aktenzeichen) if present.
    Only sets fields when a matching DB row is found; never creates records here.

    Auto-filing a batch into a case is a write to that case, so both paths
    are restricted to the owner's *editable* cases (owned ∪ EDITOR shares).
    The az_court path filters inside the query rather than checking after
    `.first()`: an Aktenzeichen is shared by both sides of the same lawsuit,
    so an unfiltered query could match a different user's Proceeding row
    first and shadow the owner's own legitimate match — a check-after-fetch
    would then just fail closed instead of finding the right one.
    """
    from app.models.database import Case, Proceeding, User
    from app.services import access_service

    internal_id = extract_internal_id_from_subject(subject)
    az_court = extract_az_court_from_subject(subject)

    owner = db.get(User, owner_id) if owner_id is not None else None
    editable = access_service.editable_case_ids(db, owner)

    if internal_id:
        case = db.query(Case).filter(Case.id == internal_id).first()
        if case and (editable is None or case.id in editable):
            batch.case_id = case.id
            if az_court:
                proc = (
                    db.query(Proceeding)
                    .filter(
                        Proceeding.case_id == case.id,
                        Proceeding.az_court == az_court,
                    )
                    .first()
                )
                if proc:
                    batch.proceeding_id = proc.id
            logger.info(
                "Batch #%d: auto-assigned to case %s via subject internal_id",
                batch.id,
                case.id,
            )
            return

    if az_court:
        proc_query = db.query(Proceeding).filter(Proceeding.az_court == az_court)
        if editable is not None:
            proc_query = proc_query.filter(Proceeding.case_id.in_(editable))
        proc = proc_query.first()
        if proc:
            batch.case_id = proc.case_id
            batch.proceeding_id = proc.id
            logger.info(
                "Batch #%d: auto-assigned to case %s via subject az_court",
                batch.id,
                proc.case_id,
            )


def ingest_raw_email(
    db: Session,
    raw_bytes: bytes,
    source_type: IngestBatchSourceType = IngestBatchSourceType.EMAIL,
    owner_id: int | None = None,
) -> IngestBatch | None:
    owner_id = _resolve_owner_id(db, owner_id)
    parsed = parse_rfc822(raw_bytes)
    msg_id = parsed["message_id"]
    sender = parsed["sender"] or "unknown"
    subject = parsed["subject"] or "No Subject"

    batch_repo = IngestBatchRepository(db)

    source_hash = None
    if msg_id:
        existing = batch_repo.get_by_message_id(msg_id, owner_id)
        if existing:
            doc_count = (
                db.query(Document)
                .filter(Document.ingest_batch_id == existing.id)
                .count()
            )
            if doc_count > 0:
                logger.info(
                    "Email duplicate: message-id %s already in batch #%d (%d docs) — skipping",
                    msg_id,
                    existing.id,
                    doc_count,
                )
                return existing
            if is_no_document_tombstone(existing):
                logger.info(
                    "Email message-id %s already ingested as a no-document batch "
                    "#%d — skipping",
                    msg_id,
                    existing.id,
                )
                return None
            logger.info(
                "Email batch #%d has 0 docs (orphaned) — deleting and re-ingesting",
                existing.id,
            )
            db.delete(existing)
            db.flush()
    else:
        # Hash the raw email bytes directly — avoids collisions from emails with the
        # same sender/subject but empty bodies (T3.11).
        fallback_hash = hashlib.sha256(raw_bytes).hexdigest()
        existing = (
            db.query(IngestBatch)
            .filter(
                IngestBatch.source_type == IngestBatchSourceType.EMAIL,
                IngestBatch.source_hash == fallback_hash,
                IngestBatch.owner_id == owner_id,
            )
            .first()
        )
        if existing:
            doc_count = (
                db.query(Document)
                .filter(Document.ingest_batch_id == existing.id)
                .count()
            )
            if doc_count > 0:
                logger.info(
                    "Email duplicate (fallback hash): already in batch #%d (%d docs) — skipping",
                    existing.id,
                    doc_count,
                )
                return existing
            if is_no_document_tombstone(existing):
                logger.info(
                    "Email (fallback hash) already ingested as a no-document "
                    "batch #%d — skipping",
                    existing.id,
                )
                return None
            db.delete(existing)
            db.flush()
            source_hash = fallback_hash
        else:
            source_hash = fallback_hash if not msg_id else None

    received_date = parsed.get("received_date")

    batch = batch_repo.create_batch(
        source_type=source_type,
        owner_id=owner_id,
        subject=subject[:255],
        sender_email=sender[:255] if sender != "unknown" else None,
        received_at=received_date,
    )
    batch.message_id = msg_id
    if source_hash:
        batch.source_hash = source_hash

    # Store attachment manifest and forwarding note extracted from the email body
    # before the body is discarded (happens below when attachments are present).
    raw_manifest = parsed.get("attachment_manifest") or []
    if raw_manifest:
        batch.attachment_manifest = raw_manifest
    raw_note = parsed.get("email_note") or ""
    if raw_note:
        batch.email_note = raw_note

    try:
        db.flush()
    except IntegrityError:
        # Two concurrent requests for the same email (e.g. Gmail sync racing
        # a manual re-sync) both passed the SELECT-based duplicate check
        # above before either committed — the new UNIQUE(owner_id,
        # message_id/source_hash) constraint is the real guard here. Losing
        # this race isn't an error: the winner's batch is the correct
        # result, so recover it and return that instead of propagating.
        db.rollback()
        existing = (
            batch_repo.get_by_message_id(msg_id, owner_id)
            if msg_id
            else batch_repo.get_by_source_hash(source_hash, owner_id)
            if source_hash
            else None
        )
        if existing is not None:
            logger.info(
                "Email batch race: lost to concurrent insert — reusing batch #%d",
                existing.id,
            )
            return existing
        raise

    # Attempt to auto-assign case from the email subject so downstream stages
    # receive a case_id/proceeding_id without waiting for AI metadata.
    _try_assign_case_from_subject(db, batch, subject, owner_id)

    logger.info(
        "Email batch #%d created: from=%s subject=%r attachments=%d",
        batch.id,
        sender,
        subject,
        len(parsed["attachments"]),
    )

    case_dir = DATA_DIR / "_TRIAGE"
    case_dir.mkdir(parents=True, exist_ok=True)
    # Attachment paths written here initially — SQLAlchemy event moves them to
    # the case/proceeding folder once confirmed.

    docs_to_process: list[Document] = []
    has_attachments = bool(parsed["attachments"])
    # Paths written below — on any exception before the commit at the end of
    # this try block, these (and only these; a dedup-reused attachment's
    # existing file is never appended) are removed so the DB rollback
    # doesn't leave orphaned files with no row pointing at them.
    written_paths: list[Path] = []

    try:
        _ingest_email_docs_and_commit(
            db,
            batch,
            parsed,
            subject,
            sender,
            owner_id,
            received_date,
            case_dir,
            has_attachments,
            docs_to_process,
            written_paths,
        )
    except Exception:
        db.rollback()
        for p in written_paths:
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        raise

    if not docs_to_process:
        return None  # recorded as a no-document tombstone above; nothing to dispatch

    logger.info(
        "Batch #%d committed — dispatching process_document_task for %d doc(s)",
        batch.id,
        len(docs_to_process),
    )
    for doc in docs_to_process:
        dispatch_task("app.tasks.document_processing.process_document_task", doc.id)

    return batch


def _ingest_email_docs_and_commit(
    db: Session,
    batch: IngestBatch,
    parsed: dict,
    subject: str,
    sender: str,
    owner_id: int | None,
    received_date,
    case_dir: Path,
    has_attachments: bool,
    docs_to_process: list[Document],
    written_paths: list[Path],
) -> None:
    """Body/attachment document creation and the final commit for
    ingest_raw_email — split out purely so the caller can wrap every raise
    point between here and the commit in one try/except for file cleanup."""
    # Create a body document only when the email itself is the content (no attachments).
    # With attachments the body is a transport cover note; metadata lives on the batch.
    if parsed["body"].strip() and not has_attachments:
        body_hash = hashlib.sha256(parsed["body"].encode()).hexdigest()
        body_path = case_dir / f"email_body_{batch.id}.txt"
        with open(body_path, "w") as f:
            f.write(parsed["body"])
        written_paths.append(body_path)

        threading_meta = None
        if parsed.get("in_reply_to") or parsed.get("references"):
            threading_meta = {
                "in_reply_to": parsed.get("in_reply_to"),
                "references": parsed.get("references"),
            }

        _subject_internal_id = extract_internal_id_from_subject(subject)
        doc = Document(
            title=subject,
            owner_id=owner_id,
            file_path=to_storage_path(body_path),
            original_filename=f"email_body_{batch.id}.txt",
            content_hash=body_hash,
            case_id=batch.case_id or "_TRIAGE",
            proceeding_id=batch.proceeding_id,
            ingest_batch_id=batch.id,
            internal_id=_subject_internal_id or None,
            sender=parsed["sender"] or None,
            received_date=received_date,
            issued_date=received_date,
            meta={"threading": threading_meta} if threading_meta else None,
            page_count=0,
        )
        from app.services.pipeline_status import initialize as _pipeline_init

        db.add(doc)
        db.flush()
        _pipeline_init(doc, batched=True, db=db)
        docs_to_process.append(doc)
        logger.info("Batch #%d: email body queued as document", batch.id)

    for att in parsed["attachments"]:
        if not att["content"] or not att["filename"]:
            continue
        att_hash = hashlib.sha256(att["content"]).hexdigest()

        # Check for duplicate within the same user's own _TRIAGE.
        existing_doc = (
            db.query(Document)
            .filter(
                Document.content_hash == att_hash,
                Document.case_id == "_TRIAGE",
                Document.owner_id == owner_id,
            )
            .first()
        )

        if existing_doc:
            # Leave it where it is: don't move it into this batch and don't
            # re-run its pipeline. This is the same PDF already ingested
            # (from an earlier email, forward, or reply-with-attachment) —
            # moving it here would tear it out of its original batch and
            # re-dispatching would burn a full re-extraction for content
            # already extracted. (The lookup above is scoped to case_id ==
            # "_TRIAGE" and owner_id == owner_id, so this never matches a doc
            # already confirmed into a real case, or another user's
            # still-untriaged duplicate — cross-user hash collisions would
            # otherwise silently drop this user's own copy of the
            # attachment, since it's the same content_hash but a document
            # they can't see.)
            logger.info(
                "Batch #%d: attachment %r is a duplicate of doc #%d already "
                "in batch #%s — leaving it in place, not re-processing",
                batch.id,
                att["filename"],
                existing_doc.id,
                existing_doc.ingest_batch_id,
            )
            continue

        safe_name = _sanitize_filename(att["filename"])
        att_path = case_dir / f"{batch.id}_{safe_name}"
        if att_path.exists():
            # Two attachments in the same email sharing a filename would
            # otherwise silently overwrite each other on disk — give the
            # second (and any further) one a disambiguating suffix.
            stem, suffix = att_path.stem, att_path.suffix
            n = 2
            while att_path.exists():
                att_path = case_dir / f"{stem}_{n}{suffix}"
                n += 1
        with open(att_path, "wb") as f:
            f.write(att["content"])
        written_paths.append(att_path)

        try:
            pdf_doc = pdfium.PdfDocument(str(att_path))
            att_page_count = len(pdf_doc)
            pdf_doc.close()
        except Exception:
            att_page_count = 0

        doc = Document(
            title=att["filename"],
            owner_id=owner_id,
            file_path=to_storage_path(att_path),
            original_filename=att["filename"],
            content_hash=att_hash,
            case_id=batch.case_id or "_TRIAGE",
            proceeding_id=batch.proceeding_id,
            ingest_batch_id=batch.id,
            internal_id=extract_internal_id_from_subject(subject) or None,
            received_date=received_date or datetime.now(UTC),
            page_count=att_page_count,
        )
        from app.services.pipeline_status import initialize as _pipeline_init

        db.add(doc)
        db.flush()
        _pipeline_init(doc, batched=True, db=db)
        docs_to_process.append(doc)
        logger.info("Batch #%d: attachment %r queued", batch.id, att["filename"])

    if docs_to_process:
        batch.status = IngestBatchStatus.PROCESSING
    else:
        # Nothing to ingest: every attachment was a duplicate already ingested
        # elsewhere, empty or unnamed (the body is discarded whenever there are
        # attachments), or the email had neither attachments nor a body. Commit
        # a COMPLETED, document-less tombstone for the message instead of
        # leaving a PENDING 0-doc batch (which nothing ever advances and
        # delete_bundle could not remove) or rolling back (which made the next
        # fetch of the same Message-ID — routine, the Gmail sync watermark
        # overlaps on purpose — re-process it, and left the Gmail import page
        # offering the message again for ever). COMPLETED is the honest status:
        # there is no bundle for the user to triage, so nothing is hidden from
        # the feed; the tombstone only records "this message was seen".
        batch.status = IngestBatchStatus.COMPLETED
        batch.meta = {**(batch.meta or {}), "reason": NO_NEW_DOCUMENTS}
        db.commit()
        logger.info(
            "Email from=%s subject=%r produced no new documents (every "
            "attachment was a duplicate, empty, or unnamed, or the message was "
            "empty) — recorded as a completed no-document batch #%d",
            sender,
            subject,
            batch.id,
        )
        return None

    # Link manifest entries to their Document IDs now that all docs are flushed.
    if batch.attachment_manifest:
        filename_to_doc_id = {
            d.original_filename: d.id for d in docs_to_process if d.original_filename
        }
        linked = False
        for entry in batch.attachment_manifest:
            entry_filename = entry.get("filename")
            doc_id = (
                filename_to_doc_id.get(entry_filename)
                if entry_filename is not None
                else None
            )
            if doc_id is not None:
                entry["doc_id"] = doc_id
                linked = True
        if linked:
            # Force SQLAlchemy to detect the JSON mutation
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(batch, "attachment_manifest")

    db.commit()


def ingest_scanned_file(
    db: Session,
    pdf_path: Path,
    batch_id: str,
    source_hash: str,
    owner_id: int | None = None,
) -> IngestBatch | None:
    """Ingest a single scanned PDF from the scan folder.

    Returns None when the file is a duplicate (already ingested).
    Returns the created IngestBatch otherwise.
    """
    owner_id = _resolve_owner_id(db, owner_id)
    batch_repo = IngestBatchRepository(db)

    existing = batch_repo.get_by_source_hash(source_hash, owner_id)
    if existing:
        logger.info("Scan duplicate: hash already in batch #%d — skipping", existing.id)
        return None

    batch = batch_repo.create_batch(
        source_type=IngestBatchSourceType.SCAN,
        owner_id=owner_id,
        subject=pdf_path.name[:255],
        raw_source_path=to_storage_path(pdf_path),
    )
    batch.source_hash = source_hash
    try:
        db.flush()
    except IntegrityError:
        # Same race as ingest_raw_email: a concurrent scan-loop run for the
        # same file could pass the SELECT-based check above before either
        # commits. Losing the race just means the file was already ingested.
        db.rollback()
        logger.info(
            "Scan duplicate (race): hash already ingested by a concurrent run — skipping"
        )
        return None

    logger.info("Scan batch #%d created: file=%s", batch.id, pdf_path.name)

    try:
        pdf_doc = pdfium.PdfDocument(str(pdf_path))
        page_count = len(pdf_doc)
        pdf_doc.close()
    except Exception as exc:
        db.rollback()
        raise ValueError(f"Cannot open PDF: {exc}") from exc

    if page_count == 1:
        # Single-page: create Document directly and dispatch
        # source_hash is already the SHA-256 of this file's bytes (the scan driver
        # computed it), so don't read the whole PDF into memory a second time.
        content_hash = source_hash
        doc = Document(
            title=pdf_path.name,
            owner_id=owner_id,
            file_path=to_storage_path(pdf_path),
            original_filename=pdf_path.name,
            content_hash=content_hash,
            case_id="_TRIAGE",
            ingest_batch_id=batch.id,
            page_count=page_count,
        )
        from app.services.pipeline_status import initialize as _pipeline_init

        db.add(doc)
        db.flush()
        _pipeline_init(doc, batched=False, db=db)
        batch.status = IngestBatchStatus.PROCESSING
        db.commit()
        logger.info("Scan batch #%d: single-page PDF, dispatching extraction", batch.id)
        dispatch_task("app.tasks.document_processing.process_document_task", doc.id)
    else:
        # Multi-page: queue slicing; no Documents yet
        batch.meta = {
            "slicing": {
                "status": "preparing",
                "page_count": page_count,
                # Lets recover_stuck_slicing_prep tell "still running" from "lost".
                "dispatched_at": now_utc().isoformat(),
            }
        }
        batch.status = IngestBatchStatus.AWAITING_SLICING
        db.commit()
        logger.info("Scan batch #%d: %d pages → queuing slicing", batch.id, page_count)
        dispatch_task("app.tasks.prepare_slicing.prepare_slicing_task", batch.id)

    return batch
