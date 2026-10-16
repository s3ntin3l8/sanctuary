"""Regression tests for attachment-level ingest bugs fixed in PR1 of the
2026-09-27 ingestion audit:

- A duplicate attachment (same content hash, already ingested from an
  earlier email) must not be moved into the new batch or re-dispatched
  through the pipeline — it stays exactly where it is.
- An email whose *only* content would have been duplicate attachments
  must not persist an empty batch — self-review on this PR found that
  doing so, combined with the Gmail-sync watermark-overlap fix (which
  deliberately refetches recent messages), churns a brand new batch ID
  on every refetch of the same message.
- Two attachments sharing a filename within the *same* email must not
  overwrite each other on disk.
"""

import email.message

import pytest

from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
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
    # Its only attachment is a full duplicate and the body is discarded
    # (attachments present) — there is nothing new to keep, so no batch is
    # persisted at all (same "nothing to do" contract as ingest_scanned_file
    # returning None for a duplicate scan).
    assert second_batch is None

    # The original document must still belong to its original batch.
    db_session.refresh(first_docs[0])
    assert first_docs[0].ingest_batch_id == first_batch.id
    assert original_doc_id == first_docs[0].id


@pytest.mark.integration
def test_reingesting_same_duplicate_only_email_does_not_churn_batch_ids(db_session):
    """Re-ingesting the exact same email (same Message-ID) whose only
    attachment duplicates an already-ingested document must not spawn a new
    batch row every time. The first sight is recorded as one COMPLETED,
    document-less tombstone (so the message counts as handled and is not
    offered again); every later re-ingest — which the Gmail-sync watermark
    overlap does deliberately, on every tick — finds that tombstone and leaves
    it alone instead of deleting and recreating it under a new id."""
    pdf_bytes = b"%PDF-1.4 already-ingested content"

    original_raw = _build_email(
        "<original@example.com>", "Original", [("schriftsatz.pdf", pdf_bytes)]
    )
    original_batch = ingest_raw_email(
        db_session, original_raw, source_type=IngestBatchSourceType.EMAIL
    )
    assert original_batch is not None

    dup_raw = _build_email(
        "<repeat-me@example.com>",
        "Weiterleitung mit gleichem Anhang",
        [("schriftsatz.pdf", pdf_bytes)],
    )
    batch_count_before = db_session.query(IngestBatch).count()

    assert (
        ingest_raw_email(db_session, dup_raw, source_type=IngestBatchSourceType.EMAIL)
        is None
    )
    tombstones = (
        db_session.query(IngestBatch)
        .filter(IngestBatch.message_id == "<repeat-me@example.com>")
        .all()
    )
    assert len(tombstones) == 1
    assert tombstones[0].status == IngestBatchStatus.CONFIRMED
    assert tombstones[0].meta["reason"] == "no_new_documents"
    assert db_session.query(IngestBatch).count() == batch_count_before + 1

    for _ in range(3):
        result = ingest_raw_email(
            db_session, dup_raw, source_type=IngestBatchSourceType.EMAIL
        )
        assert result is None

    assert db_session.query(IngestBatch).count() == batch_count_before + 1, (
        "re-ingesting the same duplicate-only email must reuse its tombstone, "
        "not delete and recreate it"
    )
    assert (
        db_session.query(IngestBatch)
        .filter(IngestBatch.message_id == "<repeat-me@example.com>")
        .one()
        .id
        == tombstones[0].id
    )


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


@pytest.mark.integration
def test_attachment_dedup_is_scoped_per_owner(db_session):
    """The same PDF bytes arriving for two *different* users must not dedup
    across them — B's copy would otherwise be silently dropped because A's
    identical-hash document already sits in the shared _TRIAGE bucket, and B
    can't see or access A's copy."""
    from app.services import auth_service

    a = auth_service.create_user(db_session, email="a@example.com", password="pw12345")
    b = auth_service.create_user(db_session, email="b@example.com", password="pw12345")
    db_session.commit()

    pdf_bytes = b"%PDF-1.4 shared content both users happen to attach"

    a_raw = _build_email(
        "<a-msg@example.com>", "A's letter", [("schriftsatz.pdf", pdf_bytes)]
    )
    a_batch = ingest_raw_email(
        db_session, a_raw, source_type=IngestBatchSourceType.EMAIL, owner_id=a.id
    )
    assert a_batch is not None

    b_raw = _build_email(
        "<b-msg@example.com>", "B's letter", [("schriftsatz.pdf", pdf_bytes)]
    )
    b_batch = ingest_raw_email(
        db_session, b_raw, source_type=IngestBatchSourceType.EMAIL, owner_id=b.id
    )
    assert b_batch is not None, (
        "B's email must still produce a batch+document of their own, not be "
        "treated as a duplicate of A's unrelated document"
    )

    b_docs = (
        db_session.query(Document).filter(Document.ingest_batch_id == b_batch.id).all()
    )
    assert len(b_docs) == 1
    assert b_docs[0].owner_id == b.id


@pytest.mark.integration
def test_resent_attachment_already_filed_in_a_case_is_not_duplicated(db_session):
    """The first copy sits in a case (the subject names it), so the dedup
    must look past triage: a re-sent PDF links to it instead of becoming a
    second document."""
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction

    db_session.add(
        Case(
            id="8372-25",
            title="Vogt",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    db_session.flush()
    pdf_bytes = b"%PDF-1.4 filed in a case"

    first = ingest_raw_email(
        db_session,
        _build_email(
            "<f1@example.com>", "8372/25 - Schriftsatz", [("s.pdf", pdf_bytes)]
        ),
        source_type=IngestBatchSourceType.EMAIL,
    )
    assert first is not None
    doc = db_session.query(Document).filter(Document.ingest_batch_id == first.id).one()
    assert doc.case_id == "8372-25"

    again = ingest_raw_email(
        db_session,
        _build_email(
            "<f2@example.com>", "AW: 8372/25 - Schriftsatz", [("s.pdf", pdf_bytes)]
        ),
        source_type=IngestBatchSourceType.EMAIL,
    )
    assert again is None
    assert (
        db_session.query(Document)
        .filter(Document.content_hash == doc.content_hash)
        .count()
        == 1
    )


@pytest.mark.integration
def test_body_document_keeps_its_header_sender_and_date_through_extraction(db_session):
    """An own mail keeps its body as a document; the regex extractors must not
    replace the From and Date headers with whatever the text mentions."""
    from app.services.ingestion.service import _apply_script_extractors

    msg = email.message.EmailMessage()
    msg["From"] = "me@example.com"
    msg["To"] = "lawyer@example.com"
    msg["Subject"] = "Frage"
    msg["Message-ID"] = "<own@example.com>"
    msg["Date"] = "Tue, 03 Mar 2026 10:00:00 +0000"
    msg.set_content("Siehe unser Schreiben vom 01.01.2020 an info@gegner.de.")
    batch = ingest_raw_email(
        db_session,
        msg.as_bytes(),
        source_type=IngestBatchSourceType.EMAIL,
        own_addresses={"me@example.com"},
    )
    assert batch is not None
    doc = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).one()

    _apply_script_extractors(
        doc, doc.content or "Siehe 01.01.2020 info@gegner.de", db_session
    )

    assert doc.sender == "me@example.com"
    assert doc.issued_date.date().isoformat() == "2026-03-03"
    assert doc.extraction_confidence["sender"] == "email_header"


@pytest.mark.integration
def test_the_same_attachment_for_a_second_case_is_not_swallowed(db_session):
    """The same Anlage can belong to two matters: a copy filed under another
    case is not this mail's duplicate."""
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction

    for case_id in ("8372-25", "9418-26"):
        db_session.add(
            Case(
                id=case_id,
                title=case_id,
                status=CaseStatus.INTAKE,
                jurisdiction=Jurisdiction.DE,
            )
        )
    db_session.flush()
    pdf_bytes = b"%PDF-1.4 one Anlage, two matters"

    first = ingest_raw_email(
        db_session,
        _build_email(
            "<c1@example.com>", "8372/25 - Anlage", [("anlage-eins.pdf", pdf_bytes)]
        ),
        source_type=IngestBatchSourceType.EMAIL,
    )
    second = ingest_raw_email(
        db_session,
        _build_email(
            "<c2@example.com>", "9418/26 - Anlage", [("anlage-eins.pdf", pdf_bytes)]
        ),
        source_type=IngestBatchSourceType.EMAIL,
    )

    assert first is not None and second is not None
    doc = db_session.query(Document).filter_by(ingest_batch_id=second.id).one()
    assert doc.case_id == "9418-26"


@pytest.mark.unit
def test_ai_sender_replaces_the_header_marker(db_session):
    from app.models.database import Document as Doc
    from app.services.ai_summary import enrich_document_with_ai

    doc = Doc(
        title="t",
        file_path="x",
        sender="Dr. Vogt <v@example.com>",
        extraction_confidence={"sender": "email_header", "issued_date": "email_header"},
    )
    db_session.add(doc)
    db_session.flush()

    enrich_document_with_ai(
        doc,
        {"sender": "Kanzlei Vogt", "confidence": {"sender": "high"}},
        db_session,
    )

    assert doc.sender == "Kanzlei Vogt"
    assert doc.extraction_confidence["sender"] == "high"
    assert doc.extraction_confidence["issued_date"] == "email_header"
