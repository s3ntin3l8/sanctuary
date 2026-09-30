"""Expanded tests for parse_rfc822 — covering multipart, encodings, edge cases."""

from datetime import UTC, datetime
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


# --- Content-ID vs. attachment disposition ---


def test_parse_rfc822_content_id_without_attachment_disposition_is_inline():
    """A Content-ID part with no attachment disposition (a cid:-referenced
    signature image, no filename) stays inline and out of the attachment
    list — unchanged baseline behaviour."""
    msg = MIMEMultipart("related")
    msg["From"] = "sig@example.com"
    msg["Message-ID"] = "<inline-001@mail>"
    msg.attach(MIMEText("See my signature below.", "plain"))
    part = MIMEApplication(b"\x89PNG fake logo bytes")
    part["Content-ID"] = "<logo123>"
    msg.attach(part)
    result = parse_rfc822(msg.as_bytes())
    assert len(result["attachments"]) == 0
    assert "See my signature" in result["body"]


def test_parse_rfc822_content_id_with_explicit_attachment_disposition_kept():
    """A part with both a Content-ID *and* an explicit `attachment`
    disposition (some clients, e.g. Apple Mail, do this for ordinary
    attachments) must not be silently dropped."""
    pdf_bytes = b"%PDF-1.4 real attachment despite Content-ID"
    msg = MIMEMultipart()
    msg["From"] = "applemail@example.com"
    msg["Message-ID"] = "<cid-attach-001@mail>"
    msg.attach(MIMEText("See attached.", "plain"))
    part = MIMEApplication(pdf_bytes, Name="Klageschrift.pdf")
    part["Content-Disposition"] = 'attachment; filename="Klageschrift.pdf"'
    part["Content-ID"] = "<F0A1B2@apple>"
    msg.attach(part)
    result = parse_rfc822(msg.as_bytes())
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["filename"] == "Klageschrift.pdf"
    assert result["attachments"][0]["content"] == pdf_bytes


# --- HTML-only body fallback ---


def test_parse_rfc822_html_only_multipart_falls_back_to_converted_text():
    """No text/plain part anywhere (HTML-only email) — the text/html part
    is converted to text instead of leaving body empty, which would
    otherwise produce a 0-document triage batch for a no-attachment email."""
    msg = MIMEMultipart("alternative")
    msg["From"] = "html@example.com"
    msg["Message-ID"] = "<html-001@mail>"
    msg.attach(MIMEText("<p>Wichtiger <b>Hinweis</b> zum Verfahren.</p>", "html"))
    result = parse_rfc822(msg.as_bytes())
    assert result["body"].strip()
    assert "Wichtiger" in result["body"]
    assert "Hinweis" in result["body"]


def test_parse_rfc822_html_only_non_multipart_falls_back():
    """A bare (non-multipart) text/html message, no wrapper at all."""
    raw = (
        b"From: html2@example.com\n"
        b"Subject: Plain HTML\n"
        b"Message-ID: <html-002@mail>\n"
        b"Content-Type: text/html\n"
        b"\n"
        b"<html><body>Bitte <strong>antworten</strong> Sie zeitnah.</body></html>"
    )
    result = parse_rfc822(raw)
    assert "antworten" in result["body"]


def test_parse_rfc822_prefers_text_plain_over_html_when_both_present():
    """multipart/alternative with both parts — text/plain still wins, HTML
    is only a fallback when there's no text/plain at all."""
    msg = MIMEMultipart("alternative")
    msg["From"] = "both@example.com"
    msg["Message-ID"] = "<both-001@mail>"
    msg.attach(MIMEText("Plain text version.", "plain"))
    msg.attach(MIMEText("<p>HTML version.</p>", "html"))
    result = parse_rfc822(msg.as_bytes())
    assert "Plain text version." in result["body"]
    assert "HTML version" not in result["body"]


def test_html_fallback_fires_when_text_plain_part_is_present_but_empty():
    """A broken/lazy mail client that sends multipart/alternative with a
    present-but-empty text/plain part alongside a real HTML part is common
    in the wild. The HTML fallback must treat "empty" the same as "absent",
    not skip the fallback just because a (useless) text/plain part exists."""
    msg = MIMEMultipart("alternative")
    msg["From"] = "emptyplain@example.com"
    msg["Message-ID"] = "<empty-plain-001@mail>"
    msg.attach(MIMEText("", "plain"))
    msg.attach(MIMEText("<p>The real content is only here.</p>", "html"))
    result = parse_rfc822(msg.as_bytes())
    assert "The real content is only here." in result["body"]


def test_html_fallback_does_not_escape_underscores_in_manifest_filenames():
    """markdownify's default backslash-escaping of underscores would corrupt
    an attachment filename in the beA/court manifest block (e.g.
    "SCHR_ LG_26.PDF" -> "SCHR\\_ LG\\_26.PDF"), breaking the manifest-entry
    to Document.original_filename correlation in batch_orchestrator. The
    HTML-fallback body must preserve underscores verbatim."""
    manifest_line = (
        'SCHR_ LG IN V_ 26_05_26.PDF: 26.05.2026 08:24 - "Landgericht Ingolstadt"'
    )
    msg = MIMEMultipart("alternative")
    msg["From"] = "court@example.com"
    msg["Message-ID"] = "<manifest-html-001@mail>"
    msg.attach(MIMEText(f"<p>{manifest_line}</p>", "html"))
    result = parse_rfc822(msg.as_bytes())

    assert "\\_" not in result["body"]
    assert "SCHR_ LG IN V_ 26_05_26.PDF" in result["body"]
    assert len(result["attachment_manifest"]) == 1
    assert result["attachment_manifest"][0]["filename"] == "SCHR_ LG IN V_ 26_05_26.PDF"


# --- parse_email_date fallbacks ---


def test_parse_email_date_empty_returns_none():
    assert parse_email_date("") is None


def test_parse_email_date_rfc822():
    dt = parse_email_date("Tue, 26 May 2026 08:24:00 +0200")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 5, 26)


def test_parse_email_date_iso_fallback():
    dt = parse_email_date("2026-05-26 08:24:00")
    assert dt == datetime(2026, 5, 26, 8, 24, 0, tzinfo=UTC)
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_parse_email_date_dotted_fallback():
    # A naive fallback result must be tagged UTC, not converted from local
    # time — a date-only header must round-trip to the same calendar date
    # regardless of host timezone.
    dt = parse_email_date("26.05.2026")
    assert dt == datetime(2026, 5, 26, tzinfo=UTC)
    assert dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day) == (2026, 5, 26)


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
