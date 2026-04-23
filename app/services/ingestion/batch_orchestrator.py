import hashlib
import logging
import threading
from pathlib import Path

import pypdfium2 as pdfium
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.repositories.ingest_batch import IngestBatchRepository
from app.services.ingestion.email_parser import parse_rfc822
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _dispatch(task_name: str, doc_id: int) -> None:
    """Fire a Celery task in a daemon thread so task_always_eager never blocks the caller."""
    threading.Thread(
        target=lambda: celery_app.send_task(task_name, args=[doc_id]),
        daemon=True,
    ).start()


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

    if msg_id:
        existing = batch_repo.get_by_message_id(msg_id)
        if existing:
            logger.info(
                "Email duplicate: message-id %s already in batch #%d — skipping",
                msg_id,
                existing.id,
            )
            return existing

    batch = batch_repo.create_batch(
        source_type=source_type,
        subject=subject[:255],
        sender_email=sender[:255] if sender != "unknown" else None,
    )
    batch.message_id = msg_id
    db.flush()

    logger.info(
        "Email batch #%d created: from=%s subject=%r attachments=%d",
        batch.id,
        sender,
        subject,
        len(parsed["attachments"]),
    )

    case_dir = DATA_DIR / "_TRIAGE"
    case_dir.mkdir(parents=True, exist_ok=True)

    docs_to_process = []
    has_attachments = bool(parsed["attachments"])

    # Create a body document only when the email itself is the content (no attachments).
    # With attachments the body is a transport cover note; metadata lives on the batch.
    if parsed["body"].strip() and not has_attachments:
        body_hash = hashlib.sha256(parsed["body"].encode()).hexdigest()
        body_path = case_dir / f"email_body_{batch.id}.txt"
        with open(body_path, "w") as f:
            f.write(parsed["body"])

        doc = Document(
            title=subject,
            file_path=str(body_path),
            content_hash=body_hash,
            case_id="_TRIAGE",
            ingest_batch_id=batch.id,
            sender=parsed["sender"] or None,
        )
        from app.services.pipeline_status import initialize as _pipeline_init

        _pipeline_init(doc, batched=True)
        db.add(doc)
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

        safe_name = "".join(c for c in att["filename"] if c.isalnum() or c in ".-_")
        att_path = case_dir / f"{batch.id}_{safe_name}"
        with open(att_path, "wb") as f:
            f.write(att["content"])

        doc = Document(
            title=att["filename"],
            file_path=str(att_path),
            content_hash=att_hash,
            case_id="_TRIAGE",
            ingest_batch_id=batch.id,
        )
        from app.services.pipeline_status import initialize as _pipeline_init

        _pipeline_init(doc, batched=True)
        db.add(doc)
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
        _dispatch("app.tasks.document_processing.process_document_task", doc.id)

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
            content_hash=content_hash,
            case_id="_TRIAGE",
            ingest_batch_id=batch.id,
        )
        from app.services.pipeline_status import initialize as _pipeline_init

        _pipeline_init(doc, batched=False)
        db.add(doc)
        batch.status = IngestBatchStatus.PROCESSING
        db.commit()
        logger.info("Scan batch #%d: single-page PDF, dispatching extraction", batch.id)
        _dispatch("app.tasks.document_processing.process_document_task", doc.id)
    else:
        # Multi-page: queue slicing; no Documents yet
        batch.meta = {"slicing": {"status": "preparing", "page_count": page_count}}
        batch.status = IngestBatchStatus.AWAITING_SLICING
        db.commit()
        logger.info("Scan batch #%d: %d pages → queuing slicing", batch.id, page_count)
        _dispatch("app.tasks.prepare_slicing.prepare_slicing_task", batch.id)

    return batch
