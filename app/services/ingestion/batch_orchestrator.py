import hashlib

from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestStatus
from app.services.ingestion.email_parser import parse_rfc822
from app.tasks.celery_app import celery_app


def ingest_raw_email(
    db: Session,
    raw_bytes: bytes,
    source_type: IngestBatchSourceType = IngestBatchSourceType.EMAIL,
) -> IngestBatch | None:
    parsed = parse_rfc822(raw_bytes)
    msg_id = parsed["message_id"]

    if msg_id:
        existing = (
            db.query(IngestBatch).filter(IngestBatch.message_id == msg_id).first()
        )
        if existing:
            return existing

    batch = IngestBatch(
        source_type=source_type,
        subject=parsed["subject"][:255] if parsed["subject"] else "No Subject",
        sender_email=parsed["sender"][:255] if parsed["sender"] else None,
        message_id=msg_id,
    )
    db.add(batch)
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
            # Link existing doc to this batch too?
            # For now, let's create a new doc entry pointing to same file to keep batch atomic
            pass

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
