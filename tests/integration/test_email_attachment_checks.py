"""Email attachments get the same size and content checks as uploads, and
autoresponder mail is recorded as handled without becoming a document."""

import email.message

import pytest

from app.models.database import Document
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.services.ingestion import converters
from app.services.ingestion.batch_orchestrator import ingest_raw_email

PDF = b"%PDF-1.4 a real looking pdf"


def _mail(attachments, message_id="<m@example.com>", headers=None, body="Anbei."):
    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = "Schriftsatz"
    msg["Message-ID"] = message_id
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    for filename, data in attachments:
        msg.add_attachment(
            data, maintype="application", subtype="octet-stream", filename=filename
        )
    return msg.as_bytes()


def _part_outcomes(batch):
    return {
        part["filename"]: (part["outcome"], part.get("reason"))
        for source in batch.email_sources
        for part in source["parts"]
        if part.get("filename")
    }


@pytest.mark.integration
def test_a_pdf_that_lost_its_extension_is_ingested_as_pdf(db_session):
    batch = ingest_raw_email(
        db_session,
        _mail([("Schriftsatz", PDF)]),
        source_type=IngestBatchSourceType.EMAIL,
    )
    doc = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).one()
    assert doc.title == "Schriftsatz.pdf"
    assert doc.file_path.endswith(".pdf")


@pytest.mark.integration
def test_content_that_contradicts_the_extension_is_not_ingested(db_session):
    batch = ingest_raw_email(
        db_session,
        _mail([("Schriftsatz.pdf", b"PK\x03\x04 actually a zip")]),
        source_type=IngestBatchSourceType.EMAIL,
    )
    assert batch.status == IngestBatchStatus.OPEN  # unsupported part needs review
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 0
    outcome, reason = _part_outcomes(batch)["Schriftsatz.pdf"]
    assert outcome == "unsupported"
    assert "does not match" in reason


@pytest.mark.integration
def test_an_oversized_attachment_is_not_ingested(db_session, monkeypatch):
    monkeypatch.setattr(converters, "MAX_FILE_SIZE", 10)
    batch = ingest_raw_email(
        db_session,
        _mail([("big.pdf", PDF)]),
        source_type=IngestBatchSourceType.EMAIL,
    )
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 0
    assert _part_outcomes(batch)["big.pdf"] == ("unsupported", "File is too large")


@pytest.mark.integration
def test_a_decompression_bomb_attachment_is_not_ingested(db_session):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\0" * (3 * 1024 * 1024))
    batch = ingest_raw_email(
        db_session,
        _mail([("bomb.docx", buffer.getvalue())]),
        source_type=IngestBatchSourceType.EMAIL,
    )
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 0
    outcome, reason = _part_outcomes(batch)["bomb.docx"]
    assert outcome == "unsupported"
    assert "compression ratio" in reason
    assert "attachment_unsupported" in batch.source_review_reasons


@pytest.mark.integration
def test_an_oversized_attachment_is_refused_before_it_is_decoded(
    db_session, monkeypatch
):
    from email.message import EmailMessage

    from app import config

    monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 1)
    decoded = []
    original = EmailMessage.get_payload

    def spy(self, i=None, decode=False):
        if decode:
            decoded.append(self.get_filename())
        return original(self, i, decode)

    monkeypatch.setattr(EmailMessage, "get_payload", spy)
    batch = ingest_raw_email(
        db_session,
        _mail([("big.pdf", b"%PDF" + b"0" * (1_500_000))]),
        source_type=IngestBatchSourceType.EMAIL,
    )
    assert "big.pdf" not in decoded
    assert _part_outcomes(batch)["big.pdf"] == ("unsupported", "File is too large")
    part = next(
        p
        for source in batch.email_sources
        for p in source["parts"]
        if p.get("filename") == "big.pdf"
    )
    assert part["size"] > 1_000_000


@pytest.mark.integration
@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-replied"},
        {"Precedence": "auto_reply"},
        {"X-Autoreply": "yes"},
    ],
)
def test_an_autoresponder_notice_is_recorded_as_handled_without_documents(
    db_session, headers
):
    raw = _mail([("anlage-eins.pdf", PDF)], headers=headers, body="Ich bin abwesend.")
    assert (
        ingest_raw_email(db_session, raw, source_type=IngestBatchSourceType.EMAIL)
        is None
    )

    from app.models.database import IngestBatch

    batch = db_session.query(IngestBatch).filter_by(message_id="<m@example.com>").one()
    assert batch.status == IngestBatchStatus.CONFIRMED
    assert batch.meta["skipped"] == "auto_replied"
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 0
    # Re-fetching the same message reuses the tombstone.
    assert (
        ingest_raw_email(db_session, raw, source_type=IngestBatchSourceType.EMAIL)
        is None
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-generated"},  # e.g. a court's receipt confirmation
        {"Precedence": "bulk"},
        {"List-Id": "<news.example.com>"},
    ],
)
def test_other_automated_mail_is_still_ingested(db_session, headers):
    batch = ingest_raw_email(
        db_session,
        _mail([("anlage-eins.pdf", PDF)], headers=headers),
        source_type=IngestBatchSourceType.EMAIL,
    )
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 1


@pytest.mark.integration
def test_a_text_file_may_open_like_a_mailbox_but_a_pdf_may_not(db_session):
    batch = ingest_raw_email(
        db_session,
        _mail(
            [
                ("notiz.txt", b"From Anwalt Text der Notiz"),
                ("schreiben.pdf", b"From Anwalt not a pdf"),
            ]
        ),
        source_type=IngestBatchSourceType.EMAIL,
    )
    docs = db_session.query(Document).filter_by(ingest_batch_id=batch.id).all()
    assert [d.original_filename for d in docs] == ["notiz.txt"]
    assert _part_outcomes(batch)["schreiben.pdf"][0] == "unsupported"
