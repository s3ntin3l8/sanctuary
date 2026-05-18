import hashlib
import logging
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pypdfium2 as pdfium
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.repositories.ingest_batch import IngestBatchRepository
from app.services.ingestion.email_parser import parse_rfc822
from app.services.ingestion.extractors import (
    extract_az_court_from_subject,
    extract_internal_id_from_subject,
)
from app.tasks.dispatch import dispatch_task

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    """Sanitize filename while preserving Unicode characters (e.g. German umlauts)."""
    if not name:
        return "unnamed"
    name = unicodedata.normalize("NFC", name)
    safe_chars = "-_.() "
    result = "".join(c if c.isalnum() or c in safe_chars else "_" for c in name)
    return result.strip() or "unnamed"


def _try_assign_case_from_subject(
    db: Session, batch: IngestBatch, subject: str
) -> None:
    """Set batch.case_id / batch.proceeding_id from the email subject line if possible.

    Tries internal_id (lawyer's file number, e.g. '8372/25') first — it maps 1:1 to
    Case.id per CLAUDE.md.  Falls back to az_court (court Aktenzeichen) if present.
    Only sets fields when a matching DB row is found; never creates records here.
    """
    from app.models.database import Case, Proceeding

    internal_id = extract_internal_id_from_subject(subject)
    az_court = extract_az_court_from_subject(subject)

    if internal_id:
        case = db.query(Case).filter(Case.id == internal_id).first()
        if case:
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
        proc = db.query(Proceeding).filter(Proceeding.az_court == az_court).first()
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
) -> IngestBatch | None:
    parsed = parse_rfc822(raw_bytes)
    msg_id = parsed["message_id"]
    sender = parsed["sender"] or "unknown"
    subject = parsed["subject"] or "No Subject"

    batch_repo = IngestBatchRepository(db)

    source_hash = None
    if msg_id:
        existing = batch_repo.get_by_message_id(msg_id)
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
            db.delete(existing)
            db.flush()
            source_hash = fallback_hash
        else:
            source_hash = fallback_hash if not msg_id else None

    received_date = parsed.get("received_date")

    batch = batch_repo.create_batch(
        source_type=source_type,
        subject=subject[:255],
        sender_email=sender[:255] if sender != "unknown" else None,
        received_at=received_date,
    )
    batch.message_id = msg_id
    if source_hash:
        batch.source_hash = source_hash
    db.flush()

    # Attempt to auto-assign case from the email subject so downstream stages
    # receive a case_id/proceeding_id without waiting for AI metadata.
    _try_assign_case_from_subject(db, batch, subject)

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

    docs_to_process = []
    has_attachments = bool(parsed["attachments"])

    # Create a body document only when the email itself is the content (no attachments).
    # With attachments the body is a transport cover note; metadata lives on the batch.
    if parsed["body"].strip() and not has_attachments:
        body_hash = hashlib.sha256(parsed["body"].encode()).hexdigest()
        body_path = case_dir / f"email_body_{batch.id}.txt"
        with open(body_path, "w") as f:
            f.write(parsed["body"])

        threading_meta = None
        if parsed.get("in_reply_to") or parsed.get("references"):
            threading_meta = {
                "in_reply_to": parsed.get("in_reply_to"),
                "references": parsed.get("references"),
            }

        _subject_internal_id = extract_internal_id_from_subject(subject)
        doc = Document(
            title=subject,
            file_path=str(body_path),
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

        # Check for duplicate within the same case (_TRIAGE)
        existing_doc = (
            db.query(Document)
            .filter(Document.content_hash == att_hash, Document.case_id == "_TRIAGE")
            .first()
        )

        if existing_doc:
            logger.info(
                "Batch #%d: attachment %r is a duplicate of doc #%d — re-linking",
                batch.id,
                att["filename"],
                existing_doc.id,
            )
            existing_doc.ingest_batch_id = batch.id
            docs_to_process.append(existing_doc)
            continue

        safe_name = _sanitize_filename(att["filename"])
        att_path = case_dir / f"{batch.id}_{safe_name}"
        with open(att_path, "wb") as f:
            f.write(att["content"])

        try:
            pdf_doc = pdfium.PdfDocument(str(att_path))
            att_page_count = len(pdf_doc)
            pdf_doc.close()
        except Exception:
            att_page_count = 0

        doc = Document(
            title=att["filename"],
            file_path=str(att_path),
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

    db.commit()

    logger.info(
        "Batch #%d committed — dispatching process_document_task for %d doc(s)",
        batch.id,
        len(docs_to_process),
    )
    for doc in docs_to_process:
        dispatch_task("app.tasks.document_processing.process_document_task", doc.id)

    return batch


def ingest_scanned_file(
    db: Session,
    pdf_path: Path,
    batch_id: str,
    source_hash: str,
) -> IngestBatch | None:
    """Ingest a single scanned PDF from the scan folder.

    Returns None when the file is a duplicate (already ingested).
    Returns the created IngestBatch otherwise.
    """
    batch_repo = IngestBatchRepository(db)

    existing = batch_repo.get_by_source_hash(source_hash)
    if existing:
        logger.info("Scan duplicate: hash already in batch #%d — skipping", existing.id)
        return None

    batch = batch_repo.create_batch(
        source_type=IngestBatchSourceType.SCAN,
        subject=pdf_path.name[:255],
        raw_source_path=str(pdf_path),
    )
    batch.source_hash = source_hash
    db.flush()

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
        content_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        doc = Document(
            title=pdf_path.name,
            file_path=str(pdf_path),
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
        batch.meta = {"slicing": {"status": "preparing", "page_count": page_count}}
        batch.status = IngestBatchStatus.AWAITING_SLICING
        db.commit()
        logger.info("Scan batch #%d: %d pages → queuing slicing", batch.id, page_count)
        dispatch_task("app.tasks.prepare_slicing.prepare_slicing_task", batch.id)

    return batch
