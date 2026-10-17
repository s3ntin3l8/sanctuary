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


def _tnef(files: dict[str | bytes, bytes]) -> bytes:
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
        title = name if isinstance(name, bytes) else name.encode()
        out += obj(2, 0x00018010, title + b"\0")  # title
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


def test_a_windows_1252_title_is_decoded_not_fatal():
    data = _tnef({"Schriftsatz Müller.pdf".encode("cp1252"): b"%PDF-1.4 x"})
    [member] = tnef_members(data)
    assert (member["name"], member["outcome"]) == ("Schriftsatz Müller.pdf", "pending")


def test_one_bad_member_fails_only_itself():

    data = _tnef({"a.pdf": b"%PDF-1.4 a", "b.pdf": b"%PDF-1.4 b"})
    real = tnef_members.__globals__["_tnef_name"]

    def flaky(attachment, index):
        if index == 0:
            raise RuntimeError("boom")
        return real(attachment, index)

    with patch("app.services.ingestion.email_archive._tnef_name", side_effect=flaky):
        bad, good = tnef_members(data)
    assert (bad["outcome"], bad["reason"]) == (
        "failed",
        "Unreadable winmail.dat member",
    )
    assert (good["name"], good["outcome"]) == ("b.pdf", "pending")


def test_a_zip_inside_a_winmail_dat_is_expanded():
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("K1.pdf", b"%PDF-1.4 k1")
    [member] = tnef_members(_tnef({"Anlagen.zip": buffer.getvalue()}))
    assert member["outcome"] == "archive"
    assert [m["name"] for m in member["members"]] == ["K1.pdf"]


def test_msg_recipients_are_comma_separated_headers():
    fake = _fake_message(to="A <a@x.de>; B <b@y.de>", cc="C <c@z.de>;", attachments=[])
    with patch("extract_msg.openMsg", return_value=fake):
        msg = email.message_from_bytes(
            email_msg.msg_to_rfc822(OLE), policy=policy.default
        )
    assert [a.addr_spec for a in msg["To"].addresses] == ["a@x.de", "b@y.de"]
    assert [a.addr_spec for a in msg["Cc"].addresses] == ["c@z.de"]


def test_heic_is_readable_by_any_pil_open(tmp_path):
    from PIL import Image

    path = tmp_path / "plain.heic"
    path.write_bytes(_heic())
    with Image.open(path) as image:  # no OCR path involved
        assert image.size == (32, 32)


class _Raising(SimpleNamespace):
    @property
    def data(self):
        raise RuntimeError("broken attachment")


def test_one_broken_attachment_keeps_the_message_readable():
    broken = _Raising(longFilename="kaputt.pdf")
    good = SimpleNamespace(data=b"%PDF-1.4 ok", longFilename="gut.pdf")
    fake = _fake_message(attachments=[broken, good])
    with patch("extract_msg.openMsg", return_value=fake):
        msg = email.message_from_bytes(
            email_msg.msg_to_rfc822(OLE), policy=policy.default
        )
    assert "Sie werden geladen." in msg.get_body(("plain",)).get_content()
    by_name = {a.get_filename(): a.get_content() for a in msg.iter_attachments()}
    assert by_name["gut.pdf"] == b"%PDF-1.4 ok"
    assert by_name["kaputt.pdf"] == b""  # an empty part: a visible review item


def test_a_broken_msg_attachment_is_a_visible_empty_part_when_ingested():
    broken = _Raising(longFilename="kaputt.pdf")
    with patch("extract_msg.openMsg", return_value=_fake_message(attachments=[broken])):
        parsed = parse_rfc822(_mail_with(OLE, "Ladung.msg", "vnd.ms-outlook"))
    child = next(s for s in parsed["sources"] if s["parent_id"] == "0")
    [part] = [p for p in child["parts"] if p["filename"] == "kaputt.pdf"]
    assert part["outcome"] == "pending" and part["size"] == 0


def test_openmsg_is_asked_to_tolerate_signed_and_broken_parts():
    from extract_msg.enums import ErrorBehavior

    with patch("extract_msg.openMsg", return_value=_fake_message()) as opened:
        email_msg.msg_to_rfc822(OLE)
    behaviour = opened.call_args.kwargs["errorBehavior"]
    assert behaviour & ErrorBehavior.STANDARDS_VIOLATION
    assert behaviour & ErrorBehavior.ATTACH_BROKEN


def test_the_nesting_limit_is_named_as_such():
    message = _fake_message(attachments=[])
    for _ in range(email_msg._MAX_EMBEDDED + 2):
        message = _fake_message(
            attachments=[SimpleNamespace(data=message, longFilename="inner.msg")]
        )
    with patch("extract_msg.openMsg", return_value=message):
        with pytest.raises(email_msg.MsgNestingLimit):
            email_msg.msg_to_rfc822(OLE)
        parsed = parse_rfc822(_mail_with(OLE, "Ladung.msg", "vnd.ms-outlook"))
    part = next(p for p in parsed["parts"] if p["filename"] == "Ladung.msg")
    assert part["reason"] == "Attached email nesting limit reached"
