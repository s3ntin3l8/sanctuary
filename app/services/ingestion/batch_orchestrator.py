import hashlib
from pathlib import Path

import pypdfium2 as pdfium
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus, IngestStatus
from app.repositories.ingest_batch import IngestBatchRepository
from app.services.ingestion.email_parser import parse_rfc822
from app.tasks.celery_app import celery_app


def ingest_raw_email(
    db: Session,
    raw_bytes: bytes,
    source_type: IngestBatchSourceType = IngestBatchSourceType.EMAIL,
) -> IngestBatch | None:
    parsed = parse_rfc822(raw_bytes)
    msg_id = parsed["message_id"]

    batch_repo = IngestBatchRepository(db)

    if msg_id:
        existing = batch_repo.get_by_message_id(msg_id)
        if existing:
            return existing

    batch = batch_repo.create_batch(
        source_type=source_type,
        subject=parsed["subject"][:255] if parsed["subject"] else "No Subject",
        sender_email=parsed["sender"][:255] if parsed["sender"] else None,
    )
    batch.message_id = msg_id
    db.flush()

    case_dir = DATA_DIR / "_TRIAGE"
    case_dir.mkdir(parents=True, exist_ok=True)

    docs_to_process = []

    # Save body
    if parsed["body"].strip():
        body_hash = hashlib.sha256(parsed["body"].encode()).hexdigest()
        body_path = case_dir / f"email_body_{batch.id}.txt"
        with open(body_path, "w") as f:
            f.write(parsed["body"])

        doc = Document(
            title="Email Body",
            file_path=str(body_path),
            content_hash=body_hash,
            case_id="_TRIAGE",
            ingest_batch_id=batch.id,
            ingest_status=IngestStatus.PENDING,
        )
        db.add(doc)
        docs_to_process.append(doc)

    for att in parsed["attachments"]:
        if not att["content"]:
            continue
        att_hash = hashlib.sha256(att["content"]).hexdigest()

        # Check for duplicate within the same case (_TRIAGE)
        existing_doc = (
            db.query(Document)
            .filter(Document.content_hash == att_hash, Document.case_id == "_TRIAGE")
            .first()
        )

        if existing_doc:
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
            ingest_status=IngestStatus.PENDING,
        )
        db.add(doc)
        docs_to_process.append(doc)

    db.commit()

    for doc in docs_to_process:
        celery_app.send_task(
            "app.tasks.document_processing.process_document_task", args=[doc.id]
        )

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
        return None

    batch = batch_repo.create_batch(
        source_type=IngestBatchSourceType.SCAN,
        subject=pdf_path.name[:255],
        raw_source_path=str(pdf_path),
    )
    batch.source_hash = source_hash
    db.flush()

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
            ingest_status=IngestStatus.PENDING,
        )
        db.add(doc)
        batch.status = IngestBatchStatus.PROCESSING
        db.commit()
        celery_app.send_task(
            "app.tasks.document_processing.process_document_task", args=[doc.id]
        )
    else:
        # Multi-page: queue slicing; no Documents yet
        batch.meta = {"slicing": {"status": "preparing", "page_count": page_count}}
        batch.status = IngestBatchStatus.AWAITING_SLICING
        db.commit()
        celery_app.send_task(
            "app.tasks.prepare_slicing.prepare_slicing_task", args=[batch.id]
        )

    return batch
