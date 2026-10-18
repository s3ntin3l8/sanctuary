"""Email attachments get the same size and content checks as uploads, and
autoresponder mail is recorded as handled without becoming a document."""

import email.message
from pathlib import Path

import pytest

import app.config
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


def _typed_mail(
    attach, body="Anbei der Schriftsatz.", message_id="<typed@example.com>"
):
    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = "Schriftsatz"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    attach(msg)
    return msg.as_bytes()


def _ingest(db, raw):
    from unittest.mock import patch

    with patch("app.services.ingestion.batch_orchestrator.dispatch_task"):
        return ingest_raw_email(db, raw, source_type=IngestBatchSourceType.EMAIL)


@pytest.mark.integration
def test_a_detached_signature_is_accounted_for_and_keeps_the_body(db_session):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                b"MIIBsignature",
                maintype="application",
                subtype="pkcs7-signature",
                filename="smime.p7s",
            ),
            body="Eigener Text ohne Anlagen.",
        ),
    )
    assert batch.source_review_reasons == ["mailbox_arrival_unavailable"]
    assert _part_outcomes(batch)["smime.p7s"] == ("signature", None)
    # The signature is not an attachment: the body is still a document.
    docs = db_session.query(Document).filter_by(ingest_batch_id=batch.id).all()
    assert [d.original_filename for d in docs] == [f"email_body_{batch.id}.txt"]


@pytest.mark.integration
def test_an_opaque_smime_body_is_a_visible_review_item(db_session):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                b"MIIBopaque",
                maintype="application",
                subtype="pkcs7-mime",
                filename="smime.p7m",
            )
        ),
    )
    assert "attachment_encrypted" in batch.source_review_reasons
    assert _part_outcomes(batch)["smime.p7m"][0] == "encrypted"
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 0


@pytest.mark.integration
def test_a_pgp_mime_message_is_a_visible_review_item(db_session):
    raw = (
        b"From: lawyer@example.com\r\nTo: client@example.com\r\nSubject: s\r\n"
        b"Message-ID: <pgp@example.com>\r\n"
        b'Content-Type: multipart/encrypted; protocol="application/pgp-encrypted"; '
        b"boundary=E\r\n\r\n"
        b"--E\r\nContent-Type: application/pgp-encrypted\r\n\r\nVersion: 1\r\n"
        b"--E\r\nContent-Type: application/octet-stream\r\n\r\n"
        b"-----BEGIN PGP MESSAGE-----\r\nabc\r\n-----END PGP MESSAGE-----\r\n--E--\r\n"
    )
    batch = _ingest(db_session, raw)
    assert "attachment_encrypted" in batch.source_review_reasons
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 0


@pytest.mark.integration
def test_an_inline_calendar_invitation_is_no_longer_dropped(db_session):
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Verhandlung\r\n"
        "DTSTART:20260526T080000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    raw = _typed_mail(lambda m: m.add_alternative(ics, subtype="calendar"))
    batch = _ingest(db_session, raw)
    parts = _part_outcomes(batch)
    [(name, (outcome, _))] = [(k, v) for k, v in parts.items() if k.endswith(".ics")]
    assert outcome == "ingested"
    doc = (
        db_session.query(Document)
        .filter_by(ingest_batch_id=batch.id, original_filename=name)
        .one()
    )
    assert doc.file_path.endswith(".ics")
    # The invitation is a sibling of the mail's own text, not a reason to
    # reduce that text to transport.
    assert (
        db_session.query(Document)
        .filter_by(
            ingest_batch_id=batch.id, original_filename=f"email_body_{batch.id}.txt"
        )
        .count()
        == 1
    )


def _zip_bytes(members):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.integration
def test_a_zip_attachment_is_unpacked_into_its_files(db_session):
    archive = _zip_bytes({"Schriftsatz.pdf": PDF, "Anlagen/K1.pdf": PDF + b" k1"})
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                archive,
                maintype="application",
                subtype="zip",
                filename="Anlagen.zip",
            ),
            body="Siehe Anlagen.",
        ),
    )
    outcomes = _part_outcomes(batch)
    assert outcomes["Anlagen.zip"] == ("archive", None)
    assert outcomes["Schriftsatz.pdf"][0] == "ingested"
    assert outcomes["K1.pdf"][0] == "ingested"
    titles = {
        d.original_filename
        for d in db_session.query(Document).filter_by(ingest_batch_id=batch.id)
    }
    assert {"Schriftsatz.pdf", "K1.pdf"} <= titles
    assert "attachment_unsupported" not in batch.source_review_reasons


@pytest.mark.integration
def test_a_zip_bomb_attachment_is_refused(db_session):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                _zip_bytes({"a.pdf": b"\0" * (3 * 1024 * 1024)}),
                maintype="application",
                subtype="zip",
                filename="bomb.zip",
            )
        ),
    )
    outcome, reason = _part_outcomes(batch)["bomb.zip"]
    assert outcome == "unsupported" and "compression ratio" in reason
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("scan.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64),
        ("scan.jpeg", b"\xff\xd8\xff\xe0" + b"0" * 64),
        ("scan.tiff", b"II*\x00" + b"0" * 64),
        ("page.html", b"<html><body>Hallo</body></html>"),
    ],
)
def test_images_and_html_are_ingested_as_documents(db_session, filename, payload):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                payload,
                maintype="application",
                subtype="octet-stream",
                filename=filename,
            ),
            message_id=f"<{filename}@example.com>",
        ),
    )
    assert _part_outcomes(batch)[filename][0] == "ingested"


@pytest.mark.integration
def test_an_image_whose_content_contradicts_its_extension_is_refused(db_session):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                b"\x89PNG\r\n\x1a\n" + b"0" * 64,
                maintype="application",
                subtype="octet-stream",
                filename="scan.jpg",
            )
        ),
    )
    outcome, reason = _part_outcomes(batch)["scan.jpg"]
    assert outcome == "unsupported" and "does not match" in reason


@pytest.mark.integration
@pytest.mark.parametrize(
    "members",
    [{}, {"__MACOSX/._a.pdf": b"x", ".DS_Store": b"x"}],
    ids=["empty", "junk"],
)
def test_a_zip_without_files_is_a_visible_review_item(db_session, members):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                _zip_bytes(members),
                maintype="application",
                subtype="zip",
                filename="leer.zip",
            ),
            message_id=f"<zip-{len(members)}@example.com>",
        ),
    )
    assert _part_outcomes(batch)["leer.zip"] == (
        "unsupported",
        "Archive contains no files",
    )
    assert "attachment_unsupported" in batch.source_review_reasons


@pytest.mark.integration
def test_a_webp_attachment_must_really_be_webp(db_session):
    ok = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"0" * 16,
                maintype="application",
                subtype="octet-stream",
                filename="scan.webp",
            ),
            message_id="<webp-ok@example.com>",
        ),
    )
    assert _part_outcomes(ok)["scan.webp"][0] == "ingested"
    bad = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                b"%PDF-1.4 actually a pdf",
                maintype="application",
                subtype="octet-stream",
                filename="fake.webp",
            ),
            message_id="<webp-bad@example.com>",
        ),
    )
    outcome, reason = _part_outcomes(bad)["fake.webp"]
    assert outcome == "unsupported" and "does not match" in reason


@pytest.mark.integration
def test_a_manifest_naming_the_zip_dates_the_documents_inside_it(db_session):
    from datetime import datetime

    archive = _zip_bytes({"Schriftsatz.pdf": PDF, "K1.pdf": PDF + b" k1"})
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                archive,
                maintype="application",
                subtype="zip",
                filename="Anlagen.zip",
            ),
            body='Anlagen:\nAnlagen.zip: 26.05.2026 08:24 - "Landgericht Ingolstadt"\n',
            message_id="<zip-manifest@example.com>",
        ),
    )
    docs = db_session.query(Document).filter_by(ingest_batch_id=batch.id).all()
    assert {d.original_filename for d in docs} >= {"Schriftsatz.pdf", "K1.pdf"}
    for doc in docs:
        assert doc.received_date.replace(tzinfo=None) == datetime(2026, 5, 26, 6, 24)
        assert doc.extraction_confidence["received_date"] == "bea_manifest"


@pytest.mark.integration
def test_an_armored_key_named_asc_is_not_mistaken_for_a_signature(db_session):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----\n",
                maintype="application",
                subtype="octet-stream",
                filename="key.asc",
            ),
            message_id="<asc-key@example.com>",
        ),
    )
    outcome, _ = _part_outcomes(batch)["key.asc"]
    assert outcome == "unsupported"  # loud, not a silently dropped "signature"
    assert "attachment_unsupported" in batch.source_review_reasons


@pytest.mark.integration
def test_an_armored_pgp_signature_named_asc_is_a_signature(db_session):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                b"-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n",
                maintype="application",
                subtype="octet-stream",
                filename="mail.asc",
            ),
            body="Eigener Text.",
            message_id="<asc-sig@example.com>",
        ),
    )
    assert _part_outcomes(batch)["mail.asc"] == ("signature", None)
    assert "attachment_unsupported" not in batch.source_review_reasons


def _tnef_bytes(files):
    import struct

    def obj(level, attribute, data):
        return (
            struct.pack("<BIl", level, attribute, len(data))
            + data
            + struct.pack("<H", sum(data) & 0xFFFF)
        )

    out = struct.pack("<IH", 0x223E9F78, 1)
    for name, data in files.items():
        out += obj(2, 0x00069002, b"\0" * 14)
        out += obj(2, 0x00018010, name.encode() + b"\0")
        out += obj(2, 0x0006800F, data)
    return out


@pytest.mark.integration
def test_a_winmail_dat_is_unpacked_into_documents(db_session):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                _tnef_bytes({"Schriftsatz.pdf": PDF, "notiz.txt": b"Eine Notiz"}),
                maintype="application",
                subtype="ms-tnef",
                filename="winmail.dat",
            ),
            message_id="<tnef@example.com>",
        ),
    )
    outcomes = _part_outcomes(batch)
    assert outcomes["winmail.dat"] == ("archive", None)
    assert outcomes["Schriftsatz.pdf"][0] == "ingested"
    assert outcomes["notiz.txt"][0] == "ingested"
    assert "attachment_unsupported" not in batch.source_review_reasons


@pytest.mark.integration
def test_a_rich_text_only_winmail_dat_is_not_a_review_item(db_session):
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                _tnef_bytes({}),
                maintype="application",
                subtype="ms-tnef",
                filename="winmail.dat",
            ),
            body="Eigener Text ohne Anlagen.",
            message_id="<tnef-empty@example.com>",
        ),
    )
    assert batch.source_review_reasons == ["mailbox_arrival_unavailable"]
    docs = db_session.query(Document).filter_by(ingest_batch_id=batch.id).all()
    assert [d.original_filename for d in docs] == [f"email_body_{batch.id}.txt"]


@pytest.mark.integration
def test_an_attached_outlook_message_and_its_files_become_documents(db_session):
    from types import SimpleNamespace
    from unittest.mock import patch

    fake = SimpleNamespace(
        sender="Gericht <poststelle@lg.example>",
        to="mandant@example.com",
        cc="",
        subject="Ladung",
        messageId="<inner-msg@lg.example>",
        date=None,
        body="Sie werden geladen.",
        htmlBody=None,
        attachments=[SimpleNamespace(data=PDF, longFilename="Ladung.pdf")],
        close=lambda: None,
    )
    with patch("extract_msg.openMsg", return_value=fake):
        batch = _ingest(
            db_session,
            _typed_mail(
                lambda m: m.add_attachment(
                    b"\xd0\xcf\x11\xe0" + b"\0" * 64,
                    maintype="application",
                    subtype="vnd.ms-outlook",
                    filename="Ladung.msg",
                ),
                message_id="<msg-outer@example.com>",
            ),
        )
    outcomes = _part_outcomes(batch)
    assert outcomes["Ladung.msg"] == ("nested_source", None)
    assert outcomes["Ladung.pdf"][0] == "ingested"
    assert "attachment_failed" not in batch.source_review_reasons


@pytest.mark.integration
def test_a_heic_attachment_is_ingested(db_session):
    import io

    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 120, 10)).save(buffer, format="HEIF")
    batch = _ingest(
        db_session,
        _typed_mail(
            lambda m: m.add_attachment(
                buffer.getvalue(),
                maintype="image",
                subtype="heic",
                filename="IMG_0042.HEIC",
            ),
            message_id="<heic@example.com>",
        ),
    )
    assert _part_outcomes(batch)["IMG_0042.HEIC"][0] == "ingested"


MAIL_FIXTURES = Path(__file__).parent.parent / "fixtures" / "mail"


def _stored_under(tmp_path, monkeypatch):
    # batch_orchestrator bound DATA_DIR at import: patch it there too, or the
    # files land in the session-wide data dir.
    monkeypatch.setattr(app.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("app.services.ingestion.batch_orchestrator.DATA_DIR", tmp_path)


def _attached_fixture(name, filename, subtype):
    return _typed_mail(
        lambda m: m.add_attachment(
            (MAIL_FIXTURES / name).read_bytes(),
            maintype="application",
            subtype=subtype,
            filename=filename,
        ),
        message_id=f"<{name}@example.com>",
    )


@pytest.mark.integration
def test_a_real_outlook_msg_is_read_as_a_nested_mail(db_session, tmp_path, monkeypatch):
    _stored_under(tmp_path, monkeypatch)
    batch = _ingest(
        db_session,
        _attached_fixture("test-outlook.msg", "Eingang.msg", "vnd.ms-outlook"),
    )
    assert _part_outcomes(batch)["Eingang.msg"] == ("nested_source", None)
    child = next(s for s in batch.email_sources if s["parent_id"] == "0")
    assert "Équipe Microsoft Outlook Express" in child["sender"]
    docs = db_session.query(Document).filter_by(ingest_batch_id=batch.id).all()
    texts = [(tmp_path / d.file_path).read_text(errors="replace") for d in docs]
    assert any("Messagerie et groupes de discussion" in t for t in texts)


@pytest.mark.integration
def test_a_real_winmail_dat_yields_its_files(db_session, tmp_path, monkeypatch):
    _stored_under(tmp_path, monkeypatch)
    batch = _ingest(
        db_session,
        _attached_fixture("testWINMAIL.dat", "winmail.dat", "ms-tnef"),
    )
    outcomes = _part_outcomes(batch)
    assert outcomes["winmail.dat"] == ("archive", None)
    for name in ("quick.html", "quick.pdf", "quick.txt"):
        assert outcomes[name] == ("ingested", None), name
    for name in ("quick.doc", "quick.xml"):  # formats the pipeline does not read
        assert outcomes[name] == ("unsupported", "File format is not supported")
    docs = db_session.query(Document).filter_by(ingest_batch_id=batch.id).all()
    by_title = {d.title: d for d in docs}
    assert "quick brown fox" in (tmp_path / by_title["quick.txt"].file_path).read_text()
    assert (tmp_path / by_title["quick.pdf"].file_path).read_bytes().startswith(b"%PDF")


@pytest.mark.integration
def test_an_incoming_mail_cannot_forge_a_nesting_failure(db_session):
    def attach(m):
        m.add_attachment(
            b"", maintype="application", subtype="pdf", filename="leer.pdf"
        )
        list(m.iter_attachments())[-1]["X-Sanctuary-Nesting-Limit"] = "1"

    batch = _ingest(db_session, _typed_mail(attach, message_id="<forged@example.com>"))
    assert _part_outcomes(batch)["leer.pdf"] == (
        "empty",
        "Attachment contains no readable bytes",
    )
