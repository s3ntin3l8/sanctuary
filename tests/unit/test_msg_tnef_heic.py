"""Outlook .msg, winmail.dat (TNEF) and HEIC attachments."""

import email
import io
import struct
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.ingestion import converters, email_msg
from app.services.ingestion.email_archive import is_tnef, tnef_members
from app.services.ingestion.email_parser import parse_rfc822

pytestmark = pytest.mark.unit

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64


def _tnef(files: dict[str, bytes]) -> bytes:
    """A minimal TNEF stream: one attachment object per file."""

    def obj(level, attribute, data):
        return (
            struct.pack("<BIl", level, attribute, len(data))
            + data
            + struct.pack("<H", sum(data) & 0xFFFF)
        )

    out = struct.pack("<IH", 0x223E9F78, 1)
    for name, data in files.items():
        out += obj(2, 0x00069002, b"\0" * 14)  # attachment render data
        out += obj(2, 0x00018010, name.encode() + b"\0")  # title
        out += obj(2, 0x0006800F, data)  # data
    return out


def _mail_with(data: bytes, filename: str, subtype: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["Message-ID"] = "<outer@example.com>"
    msg["Subject"] = "Unterlagen"
    msg.set_content("Eigener Text.")
    msg.add_attachment(data, maintype="application", subtype=subtype, filename=filename)
    return msg.as_bytes()


# --- winmail.dat -------------------------------------------------------------


def test_tnef_files_are_listed():
    data = _tnef({"Schriftsatz.pdf": b"%PDF-1.4 x", "K1.txt": b"hello"})
    assert is_tnef("winmail.dat", "application/ms-tnef", data)
    assert [(m["name"], m["data"]) for m in tnef_members(data)] == [
        ("Schriftsatz.pdf", b"%PDF-1.4 x"),
        ("K1.txt", b"hello"),
    ]


def test_a_winmail_dat_becomes_parts_of_the_mail():
    parsed = parse_rfc822(
        _mail_with(_tnef({"Schriftsatz.pdf": b"%PDF-1.4 x"}), "winmail.dat", "ms-tnef")
    )
    by_name = {p["filename"]: p for p in parsed["parts"]}
    assert by_name["winmail.dat"]["outcome"] == "archive"
    member = by_name["Schriftsatz.pdf"]
    assert (member["outcome"], member["id"].split(".")[-1]) == ("pending", "t0")
    assert [a["filename"] for a in parsed["attachments"]] == ["Schriftsatz.pdf"]


def test_a_winmail_dat_without_files_is_only_the_rich_text_copy():
    parsed = parse_rfc822(_mail_with(_tnef({}), "winmail.dat", "ms-tnef"))
    [part] = parsed["parts"]
    assert part["outcome"] == "ignored_inline"
    assert not parsed["has_attachments"]  # the mail's own text stays its body


def test_tnef_members_share_the_decompression_budget(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "MAX_EMAIL_SIZE_MB", 1)
    data = _tnef({"a.bin": b"x" * 600_000, "b.bin": b"y" * 600_000})
    first, second = tnef_members(data)
    assert first["outcome"] == "pending"
    assert (second["outcome"], second["data"]) == ("unsupported", b"")


# --- Outlook .msg ------------------------------------------------------------


def _fake_message(**overrides):
    attachment = SimpleNamespace(
        data=b"%PDF-1.4 bescheid",
        longFilename="Bescheid.pdf",
        shortFilename="BESCHE~1.PDF",
    )
    values = {
        "sender": "Gericht <poststelle@lg.example>",
        "to": "mandant@example.com",
        "cc": "",
        "subject": "Ladung\nzum Termin",
        "messageId": "<msg-1@lg.example>",
        "date": datetime(2026, 5, 26, 8, 24, tzinfo=UTC),
        "body": "Sie werden geladen.",
        "htmlBody": None,
        "attachments": [attachment],
        "close": lambda: None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_a_msg_is_rewritten_as_rfc822():
    with patch("extract_msg.openMsg", return_value=_fake_message()):
        raw = email_msg.msg_to_rfc822(OLE)
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["From"] == "Gericht <poststelle@lg.example>"
    assert msg["Subject"] == "Ladung zum Termin"  # one line
    assert msg["Message-ID"] == "<msg-1@lg.example>"
    assert msg["Date"].startswith("Tue, 26 May 2026 08:24:00")
    assert "Sie werden geladen." in msg.get_body(("plain",)).get_content()
    [attachment] = list(msg.iter_attachments())
    assert attachment.get_filename() == "Bescheid.pdf"
    assert attachment.get_content() == b"%PDF-1.4 bescheid"


def test_a_msg_with_only_html_keeps_it():
    fake = _fake_message(body="", htmlBody=b"<p>Hallo</p>", attachments=[])
    with patch("extract_msg.openMsg", return_value=fake):
        msg = email.message_from_bytes(
            email_msg.msg_to_rfc822(OLE), policy=policy.default
        )
    assert "Hallo" in msg.get_body(("html",)).get_content()


def test_an_embedded_msg_is_attached_as_a_message():
    inner = _fake_message(subject="Innen", attachments=[])
    outer = _fake_message(
        attachments=[SimpleNamespace(data=inner, longFilename="inner.msg")]
    )
    with patch("extract_msg.openMsg", return_value=outer):
        msg = email.message_from_bytes(
            email_msg.msg_to_rfc822(OLE), policy=policy.default
        )
    [embedded] = list(msg.iter_attachments())
    assert embedded.get_content_type() == "message/rfc822"
    assert embedded.get_content()["Subject"] == "Innen"


def test_bad_bytes_are_unreadable():
    with patch("extract_msg.openMsg", side_effect=ValueError("not an OLE file")):
        with pytest.raises(email_msg.MsgUnreadable):
            email_msg.msg_to_rfc822(OLE)


def test_an_attached_msg_is_read_as_a_nested_mail():
    with patch("extract_msg.openMsg", return_value=_fake_message()):
        parsed = parse_rfc822(_mail_with(OLE, "Ladung.msg", "vnd.ms-outlook"))
    outer = next(p for p in parsed["parts"] if p["filename"] == "Ladung.msg")
    assert outer["outcome"] == "nested_source"
    child = next(s for s in parsed["sources"] if s["parent_id"] == "0")
    assert child["sender"] == "Gericht <poststelle@lg.example>"
    assert [a["filename"] for a in child["attachments"]] == ["Bescheid.pdf"]


def test_an_unreadable_msg_is_a_failed_part():
    with patch("extract_msg.openMsg", side_effect=ValueError("broken")):
        parsed = parse_rfc822(_mail_with(OLE, "Ladung.msg", "vnd.ms-outlook"))
    part = next(p for p in parsed["parts"] if p["filename"] == "Ladung.msg")
    assert (part["outcome"], part["reason"]) == (
        "failed",
        "Unreadable attached Outlook message",
    )


# --- HEIC --------------------------------------------------------------------


def _heic() -> bytes:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 30, 30)).save(buffer, format="HEIF")
    return buffer.getvalue()


def test_heic_is_recognised_by_its_brand():
    assert converters.magic_extension(_heic()[:12]) == ".heic"
    assert converters.same_format(".heic", ".heif")
    assert converters.magic_extension(b"\0\0\0\x1cftypmp42") is None  # a video


def test_heic_is_converted_to_a_png_for_ocr(tmp_path):
    path = tmp_path / "scan.heic"
    path.write_bytes(_heic())
    seen = {}

    def fake_run(conv, file_path):
        seen["path"] = file_path
        with open(file_path, "rb") as handle:
            seen["magic"] = handle.read(4)
        return "Text des Schreibens", [], {"pages": 1}

    with (
        patch.object(converters, "_get_image_ocr_converter", return_value="conv"),
        patch.object(converters, "_run_conversion", side_effect=fake_run),
    ):
        result = converters.convert_file(str(path))
    assert seen["path"].endswith(".png") and seen["magic"] == b"\x89PNG"
    import os

    assert not os.path.exists(seen["path"])  # the temporary copy is gone
    assert path.exists()  # the stored original is untouched
    assert result["metadata"]["format"] == "image"
