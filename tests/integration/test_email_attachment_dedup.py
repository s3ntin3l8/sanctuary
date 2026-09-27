"""Regression tests for attachment-level ingest bugs fixed in PR1 of the
2026-09-27 ingestion audit:

- A duplicate attachment (same content hash, already ingested from an
  earlier email) must not be moved into the new batch or re-dispatched
  through the pipeline — it stays exactly where it is.
- Two attachments sharing a filename within the *same* email must not
  overwrite each other on disk.
"""

import email.message

import pytest

from app.models.database import Document
from app.models.enums import IngestBatchSourceType
from app.services.ingestion.batch_orchestrator import ingest_raw_email


def _build_email(
    message_id: str,
    subject: str,
    attachments: list[tuple[str, bytes]],
    body: str = "Anbei die Unterlagen.",
) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body)
    for filename, data in attachments:
        msg.add_attachment(
            data, maintype="application", subtype="pdf", filename=filename
        )
    return msg.as_bytes()


@pytest.mark.integration
def test_duplicate_attachment_across_emails_is_not_moved_or_reprocessed(db_session):
    """The same PDF bytes arriving in a second email (e.g. a reply that
    quotes the original attachment) must not tear the existing Document out
    of its original batch."""
    pdf_bytes = b"%PDF-1.4 identical content across both emails"

    first_raw = _build_email(
        "<first@example.com>", "Erstes Schreiben", [("schriftsatz.pdf", pdf_bytes)]
    )
    first_batch = ingest_raw_email(
        db_session, first_raw, source_type=IngestBatchSourceType.EMAIL
    )
    assert first_batch is not None
    first_docs = (
        db_session.query(Document)
        .filter(Document.ingest_batch_id == first_batch.id)
        .all()
    )
    assert len(first_docs) == 1
    original_doc_id = first_docs[0].id

    second_raw = _build_email(
        "<second@example.com>",
        "Zweites Schreiben mit gleichem Anhang",
        [("schriftsatz.pdf", pdf_bytes)],
    )
    second_batch = ingest_raw_email(
        db_session, second_raw, source_type=IngestBatchSourceType.EMAIL
    )
    assert second_batch is not None
    assert second_batch.id != first_batch.id

    # The original document must still belong to its original batch.
    db_session.refresh(first_docs[0])
    assert first_docs[0].ingest_batch_id == first_batch.id

    # The second batch must not contain a copy of the duplicate attachment —
    # it neither creates a new Document nor re-links the existing one.
    second_docs = (
        db_session.query(Document)
        .filter(Document.ingest_batch_id == second_batch.id)
        .all()
    )
    assert original_doc_id not in {d.id for d in second_docs}
    assert len(second_docs) == 0


@pytest.mark.integration
def test_same_filename_attachments_in_one_email_do_not_collide_on_disk(db_session):
    """Two attachments named identically in the same email must both be kept
    — as two Documents with two distinct files, not one overwriting the
    other."""
    content_a = b"%PDF-1.4 first document body"
    content_b = b"%PDF-1.4 completely different second document body"

    raw = _build_email(
        "<collide@example.com>",
        "Zwei Anlagen mit gleichem Namen",
        [("Schreiben.pdf", content_a), ("Schreiben.pdf", content_b)],
    )
    batch = ingest_raw_email(db_session, raw, source_type=IngestBatchSourceType.EMAIL)
    assert batch is not None

    docs = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).all()
    assert len(docs) == 2, (
        f"Expected 2 distinct documents, got {len(docs)}. A filename "
        f"collision on disk would silently drop one attachment's content."
    )

    paths = {d.file_path for d in docs}
    assert len(paths) == 2, "Both documents must be backed by distinct files"

    hashes = {d.content_hash for d in docs}
    assert len(hashes) == 2, "Documents must retain their own distinct content"
