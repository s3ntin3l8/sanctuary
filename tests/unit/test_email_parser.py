"""Expanded tests for parse_rfc822 — covering multipart, encodings, edge cases."""

from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from app.services.ingestion.email_parser import (
    _extract_email_note,
    _parse_attachment_manifest,
    parse_email_date,
    parse_rfc822,
)


def _simple_email(
    sender="test@example.com",
    subject="Test",
    message_id="<123@mail>",
    body="Body content.",
    date="Mon, 1 Jan 2026 12:00:00 +0000",
) -> bytes:
    raw = (
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Message-ID: {message_id}\n"
        f"Date: {date}\n"
        f"\n{body}"
    )
    return raw.encode()


# --- basic fields ---


def test_parse_rfc822_simple():
    result = parse_rfc822(_simple_email())
    assert result["sender"] == "test@example.com"
    assert result["subject"] == "Test"
    assert result["message_id"] == "<123@mail>"
    assert result["body"].strip() == "Body content."
    assert len(result["attachments"]) == 0


def test_parse_rfc822_missing_fields():
    """Missing headers return empty strings, not errors."""
    result = parse_rfc822(b"\nBody only.")
    assert result["sender"] == ""
    assert result["subject"] == ""
    assert result["message_id"] == ""


# --- multipart with attachment ---


def _multipart_email(
    body_text: str, attachment_name: str, attachment_bytes: bytes
) -> bytes:
    msg = MIMEMultipart()
    msg["From"] = "sender@example.com"
    msg["Subject"] = "Multipart Test"
    msg["Message-ID"] = "<mp-001@mail>"
    msg.attach(MIMEText(body_text, "plain"))
    part = MIMEApplication(attachment_bytes, Name=attachment_name)
    part["Content-Disposition"] = f'attachment; filename="{attachment_name}"'
    msg.attach(part)
    return msg.as_bytes()


def test_parse_rfc822_multipart_attachment():
    pdf_bytes = b"%PDF-1.4 fake content"
    raw = _multipart_email("See attached.", "contract.pdf", pdf_bytes)
    result = parse_rfc822(raw)
    assert result["sender"] == "sender@example.com"
    assert "See attached." in result["body"]
    assert len(result["attachments"]) == 1
    att = result["attachments"][0]
    assert att["filename"] == "contract.pdf"
    assert att["content"] == pdf_bytes


def test_parse_rfc822_multiple_attachments():
    msg = MIMEMultipart()
    msg["From"] = "multi@example.com"
    msg["Message-ID"] = "<multi-001@mail>"
    msg.attach(MIMEText("Body", "plain"))
    for name in ("a.pdf", "b.pdf"):
        part = MIMEApplication(b"bytes", Name=name)
        part["Content-Disposition"] = f'attachment; filename="{name}"'
        msg.attach(part)
    result = parse_rfc822(msg.as_bytes())
    assert len(result["attachments"]) == 2
    names = {a["filename"] for a in result["attachments"]}
    assert names == {"a.pdf", "b.pdf"}


# --- non-UTF-8 encoding ---


def test_parse_rfc822_latin1_body():
    """Body encoded in latin-1 should decode without raising."""
    body = "Bußgeld: 500 DM".encode("latin-1")  # no euro sign; that's cp1252 only
    msg = MIMEMultipart()
    msg["From"] = "enc@example.com"
    msg["Message-ID"] = "<enc-001@mail>"
    text_part = MIMEText("placeholder", "plain", "latin-1")
    # Manually inject latin-1 content
    text_part.set_payload(body)
    text_part.set_charset("iso-8859-1")
    msg.attach(text_part)
    result = parse_rfc822(msg.as_bytes())
    # Should not raise; body is a string
    assert isinstance(result["body"], str)


# --- message_id dedup contract (parser-level) ---


def test_parse_rfc822_returns_message_id_for_dedup():
    """Caller uses message_id for dedup; parser must surface it faithfully."""
    msg_id = "<dedup-99@example.com>"
    result = parse_rfc822(_simple_email(message_id=msg_id))
    assert result["message_id"] == msg_id


# --- malformed RFC-822 ---


def test_parse_rfc822_empty_bytes():
    """Completely empty input should return empty fields, not raise."""
    result = parse_rfc822(b"")
    assert result["body"] == ""
    assert result["attachments"] == []


def test_parse_rfc822_no_body_separator():
    """Header block without empty line — email module handles gracefully."""
    raw = b"From: x@y.com\nSubject: No body\n"
    result = parse_rfc822(raw)
    assert result["sender"] == "x@y.com"
    assert isinstance(result["body"], str)


def test_parse_rfc822_attachment_with_no_filename():
    """Attachment missing filename — get_filename() returns None, should not crash."""
    msg = MIMEMultipart()
    msg["From"] = "nf@example.com"
    msg["Message-ID"] = "<nf-001@mail>"
    msg.attach(MIMEText("body", "plain"))
    part = MIMEApplication(b"data")
    part["Content-Disposition"] = "attachment"
    msg.attach(part)
    result = parse_rfc822(msg.as_bytes())
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["filename"] is None


# --- parse_email_date fallbacks ---


def test_parse_email_date_empty_returns_none():
    assert parse_email_date("") is None


def test_parse_email_date_rfc822():
    dt = parse_email_date("Tue, 26 May 2026 08:24:00 +0200")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 5, 26)


def test_parse_email_date_iso_fallback():
    assert parse_email_date("2026-05-26 08:24:00") == datetime(2026, 5, 26, 8, 24, 0)


def test_parse_email_date_dotted_fallback():
    assert parse_email_date("26.05.2026") == datetime(2026, 5, 26)


@pytest.mark.parametrize("yy,century", [("26", 2026), ("80", 1980)])
def test_parse_email_date_two_digit_year_pivot(yy, century):
    assert parse_email_date(f"26.05.{yy}").year == century


def test_parse_email_date_garbage_returns_none():
    assert parse_email_date("not a date at all") is None


def test_parse_email_date_impossible_date_returns_none():
    # regex matches but datetime() rejects month 13 → None
    assert parse_email_date("26.13.2026") is None


# --- attachment manifest ---


def test_parse_attachment_manifest_extracts_entries():
    body = 'SCHR_ LG IN V_ 26_05_26.PDF: 26.05.2026 08:24 - "Landgericht Ingolstadt"'
    entries = _parse_attachment_manifest(body)
    assert len(entries) == 1
    assert entries[0]["filename"] == "SCHR_ LG IN V_ 26_05_26.PDF"
    assert entries[0]["timestamp"] == "2026-05-26T08:24"
    assert entries[0]["source_label"] == "Landgericht Ingolstadt"


def test_parse_attachment_manifest_empty_without_match():
    assert _parse_attachment_manifest("just a normal sentence") == []


# --- email note extraction ---


def test_extract_email_note_strips_anlagen_and_boilerplate():
    body = (
        "Sehr geehrte Damen und Herren,\n"
        "anbei der Schriftsatz zur Kenntnis.\n"
        "Anlagen:\n"
        'DOC.PDF: 26.05.2026 08:24 - "LG IN"\n'
        "\n"
        "Mit freundlichen Grüßen\n"
    )
    assert _extract_email_note(body) == "anbei der Schriftsatz zur Kenntnis."


def test_extract_email_note_truncates_long_text():
    note = _extract_email_note("wort " * 400)  # ~2000 chars, no boilerplate
    assert len(note) <= 802
    assert note.endswith("…")
