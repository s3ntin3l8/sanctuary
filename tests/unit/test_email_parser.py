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
    parse_message_ids,
    parse_rfc822,
    strip_quoted_history,
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
    assert result["attachments"][0]["original_filename"] is None
    assert result["attachments"][0]["filename"].startswith("attachment_")


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
    assert (
        len(result["attachments"]) == 1
    )  # An unreferenced non-image CID is not a signature
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
    assert entries[0]["timestamp"] == "2026-05-26T06:24:00+00:00"
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


def test_extract_email_note_keeps_inline_anlagen_text():
    body = (
        "anbei der Schriftsatz.\n"
        "Anlagen: Schriftsatz vom 3.3.2026, Beschluss vom 1.3.2026\n"
        "Mit freundlichen Grüßen\n"
    )
    assert _extract_email_note(body) == (
        "anbei der Schriftsatz. "
        "Anlagen: Schriftsatz vom 3.3.2026, Beschluss vom 1.3.2026"
    )


def test_extract_email_note_preserves_long_text():
    note = _extract_email_note("wort " * 400)  # ~2000 chars, no boilerplate
    assert note == ("wort " * 400).strip()


# --- threading headers ---


def test_parse_message_ids_handles_folding_junk_and_duplicates():
    header = "<a@x>\r\n\t<b@x> stray text (comment) <a@x>  <c@x>"
    assert parse_message_ids(header) == ["<a@x>", "<b@x>", "<c@x>"]


@pytest.mark.parametrize("header", [None, "", "no ids here", "<unterminated"])
def test_parse_message_ids_empty(header):
    assert parse_message_ids(header) == []


def test_parse_rfc822_threading_headers():
    raw = (
        b"From: a@example.com\n"
        b"Message-ID: <r@x>\n"
        b"In-Reply-To: <p@x>\n"
        b"References: <g@x>\n <p@x>\n"
        b"\nBody"
    )
    result = parse_rfc822(raw)
    assert result["in_reply_to"] == "<p@x>"
    assert result["thread_refs"] == ["<g@x>", "<p@x>"]


def test_parse_rfc822_in_reply_to_missing_from_references_is_appended():
    raw = b"From: a@example.com\nIn-Reply-To: <p@x>\nReferences: <g@x>\n\nBody"
    assert parse_rfc822(raw)["thread_refs"] == ["<g@x>", "<p@x>"]


def test_parse_rfc822_without_threading_headers():
    result = parse_rfc822(_simple_email())
    assert result["in_reply_to"] is None
    assert result["thread_refs"] == []


def _plain_email(body: bytes, charset: str, cte: str = "8bit") -> bytes:
    return (
        b"From: a@example.com\r\nMessage-ID: <cs@mail>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: text/plain; charset="' + charset.encode() + b'"\r\n'
        b"Content-Transfer-Encoding: " + cte.encode() + b"\r\n\r\n" + body
    )


@pytest.mark.parametrize("charset", ["iso-8859-1", "windows-1252"])
def test_parse_rfc822_keeps_umlauts_in_declared_charset(charset):
    raw = _plain_email("Bußgeld für Müller äöü".encode(charset), charset)
    assert parse_rfc822(raw)["body"] == "Bußgeld für Müller äöü"


def test_parse_rfc822_unknown_charset_does_not_drop_characters():
    raw = _plain_email("Grüße".encode("latin-1"), "x-bogus")
    assert parse_rfc822(raw)["body"] == "Grüße"


@pytest.mark.parametrize(
    "quoted",
    [
        "Am 03.05.2026 um 10:00 schrieb Dr. Vogt <v@firm.de>:\n> alt\n> mehr",
        "On Mon, May 4, 2026 at 9:00 AM Vogt <v@firm.de> wrote:\n> old",
        "-----Original Message-----\nFrom: v@firm.de\nold",
        "-----Ursprüngliche Nachricht-----\nVon: v@firm.de\nalt",
        "Von: Vogt\nGesendet: Montag, 4. Mai 2026\nAn: ich\nalt",
    ],
)
def test_strip_quoted_history_cuts_the_thread_below_the_reply(quoted):
    assert strip_quoted_history(f"Danke, passt.\n\n{quoted}") == "Danke, passt."


def test_strip_quoted_history_drops_inline_quote_lines_only():
    assert (
        strip_quoted_history("zu Punkt 1:\n> Frage\nAntwort") == "zu Punkt 1:\nAntwort"
    )


def test_strip_quoted_history_keeps_a_pure_forward():
    body = "> weitergeleitet\n> Text"
    assert strip_quoted_history(body) == body


def test_manifest_is_read_from_below_the_quoted_header():
    body = (
        "Anbei.\n\nAm 03.05.2026 schrieb Gericht <g@j.de>:\n"
        'Anlagen:\nSCHR_1.PDF: 26.05.2026 08:24 - "Landgericht Ingolstadt"\n'
    )
    raw = f"From: a@b.de\r\nMessage-ID: <m@x>\r\n\r\n{body}".encode()
    result = parse_rfc822(raw)
    assert result["body"] == "Anbei."
    assert [e["filename"] for e in result["attachment_manifest"]] == ["SCHR_1.PDF"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Montag, 26. Mai 2026 08:24", datetime(2026, 5, 26, 6, 24, tzinfo=UTC)),
        ("26.05.2026 08:24", datetime(2026, 5, 26, 6, 24, tzinfo=UTC)),
        (
            "Dienstag, 26. Mai 2026 um 08:24:10",
            datetime(2026, 5, 26, 6, 24, 10, tzinfo=UTC),
        ),
        ("Tue, May 26, 2026 at 8:24 PM", datetime(2026, 5, 26, 18, 24, tzinfo=UTC)),
        ("Tue, 26 May 2026 08:00:00 +0200", datetime(2026, 5, 26, 6, 0, tzinfo=UTC)),
        ("26. Mai 2026", datetime(2026, 5, 26, tzinfo=UTC)),
        ("kein Datum", None),
    ],
)
def test_forward_date_formats(text, expected):
    from app.services.ingestion.email_parser import parse_forward_date

    assert parse_forward_date(text) == expected


def test_german_outlook_forward_gives_original_sender_and_date():
    from app.services.ingestion.email_parser import parse_inline_forward

    body = (
        "Von: Müller, Hans [mailto:h.mueller@kanzlei.de]\n"
        "Gesendet: Montag, 26. Mai 2026 08:24\n"
        "An: mandant@example.com\nBetreff: Klageerwiderung\n\nText"
    )
    assert parse_inline_forward(body, "WG: Klageerwiderung") == {
        "sender": "Müller, Hans <h.mueller@kanzlei.de>",
        "sent_at": datetime(2026, 5, 26, 6, 24, tzinfo=UTC),
    }


def test_gmail_forward_block_gives_original_sender_and_date():
    from app.services.ingestion.email_parser import parse_inline_forward

    body = (
        "---------- Forwarded message ---------\n"
        "From: Court <poststelle@lg.de>\n"
        "Date: Tue, May 26, 2026 at 8:24 AM\n"
        "Subject: Ladung\nTo: me@example.com\n\nText"
    )
    result = parse_inline_forward(body, "Fwd: Ladung")
    assert result["sender"] == "Court <poststelle@lg.de>"
    assert result["sent_at"] == datetime(2026, 5, 26, 6, 24, tzinfo=UTC)


def test_nested_forward_uses_the_first_block():
    from app.services.ingestion.email_parser import parse_inline_forward

    body = (
        "-------- Weitergeleitete Nachricht --------\n"
        "Von: Erste <a@x.de>\nGesendet: 26.05.2026 08:24\n\n"
        "Von: Zweite <b@x.de>\nGesendet: 01.01.2026 08:00\n"
    )
    assert parse_inline_forward(body, "WG: x")["sender"] == "Erste <a@x.de>"


def test_unreadable_forward_date_keeps_the_sender_only():
    from app.services.ingestion.email_parser import parse_inline_forward

    body = "Von: Erste <a@x.de>\nGesendet: neulich\nAn: me\n"
    assert parse_inline_forward(body, "WG: x") == {
        "sender": "Erste <a@x.de>",
        "sent_at": None,
    }


@pytest.mark.parametrize(
    ("body", "subject"),
    [
        ("Siehe unten.\n\nVon: A <a@x.de>\nGesendet: 26.05.2026 08:24\n", "WG: x"),
        ("Von: A <a@x.de>\nGesendet: 26.05.2026 08:24\n", "AW: x"),
        ("Hallo", "WG: x"),
    ],
)
def test_only_a_pure_forward_is_read(body, subject):
    from app.services.ingestion.email_parser import parse_inline_forward

    assert parse_inline_forward(body, subject) is None
