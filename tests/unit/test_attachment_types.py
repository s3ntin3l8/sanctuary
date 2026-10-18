"""More email attachment types: calendar invitations, HTML, images, zip archives."""

import io
import os
import zipfile
from unittest.mock import patch

import pytest

from app.services.ingestion import converters
from app.services.ingestion.archive_guard import ArchiveRejected
from app.services.ingestion.calendar_text import ics_to_text
from app.services.ingestion.email_archive import archive_members, is_zip

pytestmark = pytest.mark.unit

ICS = (
    "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
    "SUMMARY:Mündliche Verhandlung\\, Saal 4\r\n"
    "DTSTART:20260526T080000Z\r\nDTEND;TZID=Europe/Berlin:20260526T113000\r\n"
    "LOCATION:Landgericht Ingolstadt\r\n"
    "ORGANIZER;CN=Geschäftsstelle:mailto:gs@lg.example\r\n"
    "ATTENDEE;CN=Dr. Müller:mailto:mueller@kanzlei.example\r\n"
    "DESCRIPTION:Bitte Ausweis\\nmitbringen\r\n"
    "  und Vollmacht.\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def test_invitation_is_rendered_in_local_time():
    text = ics_to_text(ICS)
    assert "# Termin: Mündliche Verhandlung, Saal 4" in text
    assert "- Beginn: 26.05.2026 10:00" in text  # 08:00Z is 10:00 in Berlin
    assert "- Ende: 26.05.2026 11:30" in text
    assert "- Ort: Landgericht Ingolstadt" in text
    assert "- Organisator: Geschäftsstelle <gs@lg.example>" in text
    assert "- Teilnehmer: Dr. Müller <mueller@kanzlei.example>" in text
    assert "Bitte Ausweis\nmitbringen und Vollmacht." in text


def test_a_cancelled_or_dateless_event_is_still_readable():
    text = ics_to_text(
        "BEGIN:VEVENT\nSUMMARY:Termin\nDTSTART;VALUE=DATE:20260526\n"
        "END:VEVENT\nMETHOD:CANCEL\n"
    )
    assert "- Beginn: 26.05.2026" in text


def test_a_calendar_without_events_keeps_its_raw_lines():
    text = ics_to_text(
        "BEGIN:VCALENDAR\nBEGIN:VTODO\nSUMMARY:Frist prüfen\nEND:VTODO\nEND:VCALENDAR"
    )
    assert "SUMMARY:Frist prüfen" in text


def test_a_colon_inside_a_quoted_parameter_does_not_split_the_value():
    text = ics_to_text(
        "BEGIN:VEVENT\nSUMMARY:T\n"
        'ATTENDEE;CN="Kanzlei: Berlin":mailto:x@y.de\nEND:VEVENT\n'
    )
    assert "- Teilnehmer: Kanzlei: Berlin <x@y.de>" in text


def test_convert_file_reads_an_ics(tmp_path):
    path = tmp_path / "invite.ics"
    path.write_text(ICS, encoding="utf-8")
    result = converters.convert_file(str(path))
    assert "Mündliche Verhandlung" in result["content"]


def test_convert_file_runs_an_image_through_the_ocr_pipeline(tmp_path):
    path = tmp_path / "scan.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    with (
        patch.object(converters, "_get_image_ocr_converter", return_value="conv"),
        patch.object(
            converters,
            "_run_conversion",
            return_value=("Text des Schreibens", [], {"pages": 1, "format": "image"}),
        ) as run,
    ):
        result = converters.convert_file(str(path))
    run.assert_called_once_with("conv", str(path))
    assert result["content"] == "Text des Schreibens"
    assert result["metadata"]["format"] == "image"


def test_html_goes_through_the_document_converter(tmp_path):
    path = tmp_path / "mail.html"
    path.write_text("<html><body><h1>Ladung</h1></body></html>", encoding="utf-8")
    with patch.object(
        converters, "_convert_document", return_value={"content": "# Ladung"}
    ) as convert:
        assert converters.convert_file(str(path))["content"] == "# Ladung"
    convert.assert_called_once_with(str(path))


@pytest.mark.parametrize(
    ("magic", "ext", "same"),
    [
        (".jpg", ".jpeg", True),
        (".tif", ".tiff", True),
        (".png", ".png", True),
        (".png", ".jpg", False),
    ],
)
def test_image_extension_aliases(magic, ext, same):
    assert converters.same_format(magic, ext) is same


def _zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_zip_members_are_listed_without_junk():
    data = _zip(
        {
            "Schriftsatz.pdf": b"%PDF-1.4 x",
            "Anlagen/K1.pdf": b"%PDF-1.4 y",
            "__MACOSX/._K1.pdf": b"junk",
            ".DS_Store": b"junk",
            "Anlagen/": b"",
        }
    )
    members = archive_members(data)
    assert [(m["name"], m["outcome"]) for m in members] == [
        ("Schriftsatz.pdf", "pending"),
        ("K1.pdf", "pending"),
    ]
    assert members[0]["data"] == b"%PDF-1.4 x"


def test_a_nested_zip_is_expanded_once_and_then_refused():
    inner = _zip({"deep.pdf": b"%PDF-1.4"})
    middle = _zip({"inner.zip": inner})
    outer = _zip({"middle.zip": middle})
    [member] = archive_members(outer)
    assert member["outcome"] == "archive"
    [nested] = member["members"]
    assert (nested["name"], nested["outcome"], nested["reason"]) == (
        "inner.zip",
        "unsupported",
        "Archive nesting limit",
    )


def test_a_member_larger_than_the_limit_is_not_read(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "MAX_FILE_SIZE_MB", 1)
    noisy = os.urandom(1_500_000)
    [member] = archive_members(_zip({"big.pdf": noisy}))
    assert (member["outcome"], member["reason"], member["data"]) == (
        "unsupported",
        "File is too large",
        b"",
    )


def test_a_zip_bomb_is_refused():
    with pytest.raises(ArchiveRejected):
        archive_members(_zip({"a.pdf": b"\0" * (3 * 1024 * 1024)}))


def test_is_zip_needs_a_name_or_type_and_the_magic():
    assert is_zip("a.zip", "", b"PK\x03\x04rest")
    assert is_zip(None, "application/zip", b"PK\x03\x04rest")
    assert not is_zip("a.zip", "", b"not a zip")
    assert not is_zip("a.docx", "", b"PK\x03\x04rest")


def test_an_empty_or_junk_only_zip_has_no_members():
    assert (
        archive_members(_zip({"__MACOSX/._a": b"x", ".DS_Store": b"x", "dir/": b""}))
        == []
    )


def test_a_nested_zip_without_files_is_refused_loudly():
    outer = _zip({"empty.zip": _zip({".DS_Store": b"x"})})
    [member] = archive_members(outer)
    assert (member["outcome"], member["reason"]) == (
        "unsupported",
        "Archive contains no files",
    )


def test_webp_needs_the_riff_container_name():
    assert converters.magic_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"
    assert converters.magic_extension(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None


def test_members_of_a_zip_named_in_the_manifest_share_its_group():
    from app.models.database import Document, IngestBatch
    from app.services.ingestion.email_sources import bind_manifests, source_groups

    source = {
        "id": "0",
        "parts": [
            {
                "id": "0.1",
                "filename": "Anlagen.zip",
                "outcome": "archive",
                "document_ids": [],
            },
            {
                "id": "0.1.z0",
                "filename": "a.pdf",
                "outcome": "ingested",
                "document_ids": [10],
            },
            {
                "id": "0.1.z1",
                "filename": "b.pdf",
                "outcome": "ingested",
                "document_ids": [11],
            },
        ],
        "attachment_manifest": [
            {
                "filename": "Anlagen.zip",
                "timestamp": "2026-05-26T06:24:00+00:00",
                "source_label": "Landgericht",
                "historical": False,
                "issue": None,
            }
        ],
    }
    assert bind_manifests([source]) == []
    batch = IngestBatch(email_sources=[source])
    docs = [
        Document(id=10, email_source_id="0", email_part_id="0.1.z0"),
        Document(id=11, email_source_id="0", email_part_id="0.1.z1"),
    ]
    groups, blocked = source_groups(batch, docs)
    assert not blocked
    assert groups[10] == groups[11] == source["attachment_manifest"][0]["group_id"]


def test_members_share_one_decompression_budget_per_mail(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "MAX_EMAIL_SIZE_MB", 1)
    data = _zip({"a.pdf": os.urandom(600_000), "b.pdf": os.urandom(600_000)})
    first, second = archive_members(data)
    assert first["outcome"] == "pending" and len(first["data"]) == 600_000
    assert (second["outcome"], second["data"]) == ("unsupported", b"")
    assert "size limit" in second["reason"]


def test_a_budget_spans_nested_archives():
    inner = _zip({"deep.pdf": os.urandom(500_000)})
    outer = _zip({"inner.zip": inner, "b.pdf": os.urandom(500_000)})
    inner_member, sibling = archive_members(outer, budget=[700_000])
    # Unpacking the inner zip used up most of the budget: its own file no longer fits.
    [deep] = inner_member["members"]
    assert (deep["outcome"], deep["data"]) == ("unsupported", b"")
    assert (sibling["outcome"], sibling["data"]) == ("unsupported", b"")


# --- Small images (logos, icons) -----------------------------------------------


def _image(size, fmt="PNG"):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, fmt)
    return buffer.getvalue()


def test_a_small_image_is_decorative():
    assert converters.is_decorative_image(_image((200, 80)))
    assert converters.is_decorative_image(_image((150, 150), "JPEG"))


def test_a_page_sized_image_is_not_decorative():
    assert not converters.is_decorative_image(_image((1200, 1600)))
    assert not converters.is_decorative_image(_image((1240, 1754)))


def test_unreadable_or_non_image_bytes_are_not_decorative():
    assert not converters.is_decorative_image(b"\x89PNG\r\n\x1a\n" + b"garbage")
    assert not converters.is_decorative_image(b"%PDF-1.4 small")
    assert not converters.is_decorative_image(b"")


def _mail_with_image(data: bytes, filename: str = "logo.png") -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["Message-ID"] = "<img@example.com>"
    msg["Subject"] = "Frist"
    msg.set_content("Bitte bis Freitag antworten.")
    msg.add_attachment(data, maintype="image", subtype="png", filename=filename)
    return msg.as_bytes()


def test_a_small_attached_logo_is_ignored_and_the_body_stays_a_body():
    from app.services.ingestion.email_parser import parse_rfc822

    parsed = parse_rfc822(_mail_with_image(_image((200, 80))))
    part = next(p for p in parsed["parts"] if p["filename"] == "logo.png")
    assert part["outcome"] == "ignored_inline"
    assert part["reason"] == "Small image (logo or icon)"
    assert parsed["attachments"] == []
    assert not parsed["has_attachments"]


def test_a_page_sized_image_attachment_stays_pending():
    from app.services.ingestion.email_parser import parse_rfc822

    parsed = parse_rfc822(_mail_with_image(_image((1240, 1754)), "scan.png"))
    part = next(p for p in parsed["parts"] if p["filename"] == "scan.png")
    assert part["outcome"] == "pending"
    assert [a["filename"] for a in parsed["attachments"]] == ["scan.png"]


def test_a_small_image_in_a_zip_is_ignored():
    from email.message import EmailMessage

    from app.services.ingestion.email_parser import parse_rfc822

    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["Message-ID"] = "<zip@example.com>"
    msg.set_content("Anlagen")
    msg.add_attachment(
        _zip({"logo.png": _image((120, 60)), "K1.pdf": b"%PDF-1.4 k1"}),
        maintype="application",
        subtype="zip",
        filename="Anlagen.zip",
    )
    parsed = parse_rfc822(msg.as_bytes())
    outcomes = {p["filename"]: p["outcome"] for p in parsed["parts"]}
    assert outcomes["logo.png"] == "ignored_inline"
    assert outcomes["K1.pdf"] == "pending"
    assert [a["filename"] for a in parsed["attachments"]] == ["K1.pdf"]


def test_a_zip_holding_only_a_logo_is_ignored_as_a_whole():
    from email.message import EmailMessage

    from app.services.ingestion.email_parser import parse_rfc822

    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["Message-ID"] = "<ziplogo@example.com>"
    msg.set_content("Anlagen")
    msg.add_attachment(
        _zip({"logo.png": _image((120, 60))}),
        maintype="application",
        subtype="zip",
        filename="Anlagen.zip",
    )
    parsed = parse_rfc822(msg.as_bytes())
    assert {p["outcome"] for p in parsed["parts"]} == {"ignored_inline"}
    assert parsed["attachments"] == []
    assert not parsed["has_attachments"]  # the mail's text stays its body


def _bodyless(*attachments):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["Message-ID"] = "<nobody@example.com>"
    for data, maintype, subtype, filename in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def test_a_lone_small_image_without_any_other_content_is_kept():
    from app.services.ingestion.email_parser import parse_rfc822

    parsed = parse_rfc822(
        _bodyless((_image((200, 80)), "image", "png", "snapshot.png"))
    )
    [part] = parsed["parts"]
    assert (part["outcome"], part["reason"]) == ("pending", None)
    assert [a["filename"] for a in parsed["attachments"]] == ["snapshot.png"]


def test_a_logo_next_to_a_pdf_is_still_ignored_without_a_body():
    from app.services.ingestion.email_parser import parse_rfc822

    parsed = parse_rfc822(
        _bodyless(
            (_image((200, 80)), "image", "png", "logo.png"),
            (b"%PDF-1.4 x", "application", "pdf", "Brief.pdf"),
        )
    )
    outcomes = {p["filename"]: p["outcome"] for p in parsed["parts"]}
    assert outcomes == {"logo.png": "ignored_inline", "Brief.pdf": "pending"}
    assert [a["filename"] for a in parsed["attachments"]] == ["Brief.pdf"]


def test_a_lone_zip_of_small_images_without_a_body_is_kept():
    from app.services.ingestion.email_parser import parse_rfc822

    parsed = parse_rfc822(
        _bodyless(
            (_zip({"logo.png": _image((120, 60))}), "application", "zip", "A.zip")
        )
    )
    outcomes = {p["filename"]: p["outcome"] for p in parsed["parts"]}
    assert outcomes == {"A.zip": "archive", "logo.png": "pending"}
    assert [a["filename"] for a in parsed["attachments"]] == ["logo.png"]


def test_a_lone_small_image_next_to_a_signature_file_is_kept():
    from app.services.ingestion.email_parser import parse_rfc822

    parsed = parse_rfc822(
        _bodyless(
            (_image((200, 80)), "image", "png", "snapshot.png"),
            (b"sig", "application", "pgp-signature", "signature.asc"),
        )
    )
    outcomes = {p["filename"]: p["outcome"] for p in parsed["parts"]}
    assert outcomes["snapshot.png"] == "pending"
    assert outcomes["signature.asc"] == "signature"


def test_a_lone_small_image_next_to_an_empty_attached_mail_is_kept():
    from email.message import EmailMessage

    from app.services.ingestion.email_parser import parse_rfc822

    empty = EmailMessage()
    empty["From"] = "a@example.com"
    empty["Subject"] = "leer"
    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["Message-ID"] = "<emptynested@example.com>"
    msg.add_attachment(
        _image((200, 80)), maintype="image", subtype="png", filename="snapshot.png"
    )
    msg.add_attachment(empty.as_bytes(), maintype="message", subtype="rfc822")
    parsed = parse_rfc822(msg.as_bytes())
    outcomes = {p["filename"]: p["outcome"] for p in parsed["parts"]}
    assert outcomes["snapshot.png"] == "pending"


def test_an_empty_attached_mail_does_not_turn_the_body_into_transport():
    from email.message import EmailMessage

    from app.services.ingestion.email_parser import parse_rfc822

    empty = EmailMessage()
    empty["From"] = "a@example.com"
    empty["Subject"] = "leer"
    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["Message-ID"] = "<bodyempty@example.com>"
    msg.set_content("Bitte prüfen.")
    msg.add_attachment(empty.as_bytes(), maintype="message", subtype="rfc822")
    parsed = parse_rfc822(msg.as_bytes())
    assert not parsed["has_attachments"]
    assert parsed["body"].strip() == "Bitte prüfen."
