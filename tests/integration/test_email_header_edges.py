"""End-to-end: threading headers survive ingest and become a reply edge (#58)."""

import email.message

import pytest

from app.models.database import Document, DocumentRelationship
from app.models.enums import (
    IngestBatchSourceType,
    RelationshipConfidence,
    RelationshipType,
)
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.intelligence.thread_header_linker import link_batch


def _email(message_id, *, in_reply_to=None, references=None, attachment=None):
    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = "Schriftsatz im Verfahren ADV-024-A"
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content("Anbei der Schriftsatz.")
    if attachment:
        msg.add_attachment(
            attachment[1],
            maintype="application",
            subtype="pdf",
            filename=attachment[0],
        )
    return msg.as_bytes()


@pytest.mark.integration
def test_headers_are_kept_on_the_batch_even_when_the_body_is_dropped(db_session):
    raw = _email(
        "<reply@example.com>",
        in_reply_to="<orig@example.com>",
        references="<root@example.com> <orig@example.com>",
        attachment=("brief.pdf", b"%PDF-1.4 reply"),
    )

    batch = ingest_raw_email(db_session, raw, source_type=IngestBatchSourceType.EMAIL)

    assert batch.in_reply_to == "<orig@example.com>"
    assert batch.thread_refs == ["<root@example.com>", "<orig@example.com>"]
    docs = db_session.query(Document).filter(Document.ingest_batch_id == batch.id)
    assert all((d.meta or {}).get("threading") is None for d in docs)


@pytest.mark.integration
def test_reply_with_attachment_is_linked_to_original(db_session):
    original = ingest_raw_email(
        db_session,
        _email("<orig@example.com>", attachment=("erst.pdf", b"%PDF-1.4 first")),
        source_type=IngestBatchSourceType.EMAIL,
    )
    reply = ingest_raw_email(
        db_session,
        _email(
            "<reply@example.com>",
            in_reply_to="<orig@example.com>",
            references="<orig@example.com>",
            attachment=("zweit.pdf", b"%PDF-1.4 second"),
        ),
        source_type=IngestBatchSourceType.EMAIL,
    )

    assert link_batch(db_session, reply.id) == 1
    db_session.commit()

    (edge,) = db_session.query(DocumentRelationship).all()
    from_doc = db_session.get(Document, edge.from_document_id)
    to_doc = db_session.get(Document, edge.to_document_id)
    assert from_doc.ingest_batch_id == reply.id
    assert to_doc.ingest_batch_id == original.id
    assert edge.relationship_type == RelationshipType.REPLIES_TO
    assert edge.confidence == RelationshipConfidence.EMAIL_HEADER
