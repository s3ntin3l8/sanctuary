"""Regression tests for the CLAUDE.md invariant:

> Email body is transport-only. When an email has attachments, the email body
> is intentionally discarded during ingest — the body is a cover note only;
> all substantive correspondence from the lawyer arrives as attached PDF
> letters. Do not "fix" this.

The one exception is the user's own mail (sender in ``own_addresses``): their
text is the substance, so its body is kept next to the attachments.

This test pins that contract so a future "improvement" that creates a body
document for emails with attachments fails loudly.
"""

import email.message

import pytest

from app.models.database import Document
from app.models.enums import IngestBatchSourceType
from app.services.ingestion.batch_orchestrator import ingest_raw_email


def _build_email(body: str, attachments: list[tuple[str, bytes]]) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = "Schriftsatz im Verfahren ADV-024-A"
    msg["Message-ID"] = "<test-msg-001@example.com>"
    msg.set_content(body)
    for filename, data in attachments:
        msg.add_attachment(
            data, maintype="application", subtype="pdf", filename=filename
        )
    return msg.as_bytes()


@pytest.mark.integration
def test_body_discarded_when_email_has_attachments(db_session):
    raw = _build_email(
        body="Sehr geehrte Damen und Herren,\n\nim Anhang.\n\nMit freundlichen Grüßen",
        attachments=[("schriftsatz.pdf", b"%PDF-1.4 dummy pdf content")],
    )

    batch = ingest_raw_email(db_session, raw, source_type=IngestBatchSourceType.EMAIL)

    assert batch is not None
    docs = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).all()
    assert len(docs) == 1, (
        f"Expected exactly 1 doc (the PDF attachment), got {len(docs)}: "
        f"{[d.title for d in docs]}. The email body must be discarded when "
        f"attachments are present (CLAUDE.md invariant)."
    )
    assert docs[0].title == "schriftsatz.pdf"


@pytest.mark.integration
def test_body_kept_for_own_mail_with_attachments(db_session):
    """The user's own mail is their writing: its body is kept next to the PDF."""
    raw = _build_email(
        body="Anbei meine Stellungnahme zu Ihrem Entwurf.",
        attachments=[("stellungnahme.pdf", b"%PDF-1.4 dummy pdf content")],
    ).replace(b"lawyer@example.com", b"Me@Example.com", 1)

    batch = ingest_raw_email(db_session, raw, own_addresses=["me@example.com"])

    assert batch is not None
    titles = {
        d.title
        for d in db_session.query(Document).filter(Document.ingest_batch_id == batch.id)
    }
    assert len(titles) == 2 and "stellungnahme.pdf" in titles


@pytest.mark.integration
def test_body_still_discarded_for_other_senders_with_own_addresses_set(db_session):
    raw = _build_email(
        body="Cover note.",
        attachments=[("schriftsatz.pdf", b"%PDF-1.4 dummy pdf content")],
    )
    batch = ingest_raw_email(db_session, raw, own_addresses=["me@example.com"])
    assert batch is not None
    assert (
        db_session.query(Document).filter(Document.ingest_batch_id == batch.id).count()
        == 1
    )


@pytest.mark.integration
def test_body_kept_when_email_has_no_attachments(db_session):
    raw = _build_email(
        body="This is a substantive email with no attachments — the body IS the content.",
        attachments=[],
    )

    batch = ingest_raw_email(db_session, raw, source_type=IngestBatchSourceType.EMAIL)

    assert batch is not None
    docs = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).all()
    assert len(docs) == 1, (
        "Body-only emails (no attachments) should produce exactly 1 doc "
        "containing the body text."
    )
