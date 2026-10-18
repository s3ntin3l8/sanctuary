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
import tnefparse

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


def test_a_small_image_in_a_winmail_dat_is_ignored_and_the_pdf_is_kept():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 80), "white").save(buffer, "PNG")
    parsed = parse_rfc822(
        _mail_with(
            _tnef(
                {"image001.png": buffer.getvalue(), "Schriftsatz.pdf": b"%PDF-1.4 x"}
            ),
            "winmail.dat",
            "ms-tnef",
        )
    )
    by_name = {p["filename"]: p for p in parsed["parts"]}
    assert by_name["image001.png"]["outcome"] == "ignored_inline"
    assert by_name["image001.png"]["reason"] == "Small image (logo or icon)"
    assert by_name["Schriftsatz.pdf"]["outcome"] == "pending"
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
    real = tnefparse.TNEF

    class Flaky:
        def __init__(self, attachment, broken):
            self._attachment, self._broken = attachment, broken

        @property
        def data(self):
            if self._broken:
                raise RuntimeError("boom")
            return self._attachment.data

        def __getattr__(self, name):
            return getattr(self._attachment, name)

    def parse(raw, do_checksum=False):
        parsed = real(raw, do_checksum=do_checksum)
        parsed.attachments = [
            Flaky(a, broken=(i == 0)) for i, a in enumerate(parsed.attachments)
        ]
        return parsed

    with patch("tnefparse.TNEF", side_effect=parse):
        bad, good = tnef_members(data)
    assert (bad["outcome"], bad["reason"]) == (
        "failed",
        "Unreadable winmail.dat member",
    )
    assert (good["name"], good["outcome"]) == ("b.pdf", "pending")


def test_a_name_that_cannot_be_read_keeps_the_content():
    data = _tnef({"a.pdf": b"%PDF-1.4 a"})
    with patch(
        "app.services.ingestion.email_archive._tnef_name", side_effect=RuntimeError("x")
    ):
        [member] = tnef_members(data)
    assert (member["name"], member["data"], member["outcome"]) == (
        "attachment_0",
        b"%PDF-1.4 a",
        "pending",
    )


def test_an_empty_member_is_kept_so_ingest_can_flag_it():
    [member] = tnef_members(_tnef({"leer.pdf": b""}))
    assert (member["name"], member["size"], member["outcome"]) == (
        "leer.pdf",
        0,
        "pending",
    )
    # ...and the winmail.dat is then not mistaken for the body's rich-text copy
    parsed = parse_rfc822(
        _mail_with(_tnef({"leer.pdf": b""}), "winmail.dat", "ms-tnef")
    )
    assert (
        next(p for p in parsed["parts"] if p["filename"] == "winmail.dat")["outcome"]
        == "archive"
    )


def test_a_zip_inside_a_winmail_dat_is_expanded():
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("K1.pdf", b"%PDF-1.4 k1")
    [member] = tnef_members(_tnef({"Anlagen.zip": buffer.getvalue()}))
    assert member["outcome"] == "archive"
    assert [m["name"] for m in member["members"]] == ["K1.pdf"]


def test_a_zip_member_of_a_winmail_dat_is_tagged_as_a_zip_member():
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("K1.pdf", b"%PDF-1.4 k1")
    parsed = parse_rfc822(
        _mail_with(_tnef({"Anlagen.zip": buffer.getvalue()}), "winmail.dat", "ms-tnef")
    )
    ids = {p["filename"]: p["id"] for p in parsed["parts"]}
    assert ids["K1.pdf"] == ids["winmail.dat"] + ".t0.z0"


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


def _nested(levels: int):
    """A message with a sibling file, wrapped ``levels`` times in .msg files."""
    message = _fake_message(attachments=[])
    for _ in range(levels):
        message = _fake_message(
            attachments=[
                SimpleNamespace(data=message, longFilename="inner.msg"),
                SimpleNamespace(data=b"%PDF-1.4 ok", longFilename="gut.pdf"),
            ]
        )
    return message


def test_the_nesting_limit_fails_only_the_offending_attachment():
    with patch(
        "extract_msg.openMsg", return_value=_nested(email_msg._MAX_EMBEDDED + 2)
    ):
        parsed = parse_rfc822(_mail_with(OLE, "Ladung.msg", "vnd.ms-outlook"))
    outer = next(p for p in parsed["parts"] if p["filename"] == "Ladung.msg")
    assert outer["outcome"] == "nested_source"  # the outer message survives
    [limited] = [
        p
        for source in parsed["sources"]
        for p in source["parts"]
        if p["outcome"] == "failed"
    ]
    assert limited["reason"] == "Attached email nesting limit reached"
    assert limited["mime_type"] == "application/vnd.ms-outlook"
    # every level above it keeps its sibling file readable
    assert (
        sum(
            p["filename"] == "gut.pdf" and p["outcome"] == "pending"
            for source in parsed["sources"]
            for p in source["parts"]
        )
        == email_msg._MAX_EMBEDDED
    )


def test_a_msg_beyond_the_limit_itself_is_refused():
    with patch("extract_msg.openMsg", return_value=_fake_message()):
        with pytest.raises(email_msg.MsgNestingLimit):
            email_msg.msg_to_rfc822(OLE, depth=email_msg._MAX_EMBEDDED + 1)


def test_a_msg_inside_a_nested_rfc822_mail_continues_the_depth():
    inner = EmailMessage()
    inner["From"] = "x@example.com"
    inner["Message-ID"] = "<inner@example.com>"
    inner.set_content("innen")
    inner.add_attachment(
        OLE, maintype="application", subtype="vnd.ms-outlook", filename="tief.msg"
    )
    outer = EmailMessage()
    outer["From"] = "y@example.com"
    outer["Message-ID"] = "<outer@example.com>"
    outer.set_content("aussen")
    outer.add_attachment(inner)
    depths = []

    with patch("app.services.ingestion.email_parser.msg_to_rfc822") as convert:
        convert.side_effect = lambda data, depth, nonce: (
            depths.append(depth) or b"From: a@b.de\r\n\r\nx"
        )
        parse_rfc822(outer.as_bytes())
    assert depths == [2]  # the nested mail is level 1, the .msg inside it level 2


def test_a_broken_placeholder_keeps_the_attachments_type():
    broken = _Raising(longFilename="kaputt.pdf")
    with patch("extract_msg.openMsg", return_value=_fake_message(attachments=[broken])):
        msg = email.message_from_bytes(
            email_msg.msg_to_rfc822(OLE), policy=policy.default
        )
    [placeholder] = msg.iter_attachments()
    assert placeholder.get_content_type() == "application/pdf"
    assert placeholder.get_content() == b""


def test_a_broken_attachment_without_data_is_a_visible_empty_part():
    # extract-msg's Broken/UnsupportedAttachment return data=None, they do not raise.
    unsupported = SimpleNamespace(data=None, longFilename="kaputt.pdf")
    empty = SimpleNamespace(data=b"", longFilename="leer.pdf")
    fake = _fake_message(attachments=[unsupported, empty])
    with patch("extract_msg.openMsg", return_value=fake):
        msg = email.message_from_bytes(
            email_msg.msg_to_rfc822(OLE), policy=policy.default
        )
    by_name = {a.get_filename(): a.get_content() for a in msg.iter_attachments()}
    assert by_name == {"kaputt.pdf": b"", "leer.pdf": b""}


# --- HEIC orientation --------------------------------------------------------


def _oriented(path, fmt: str, orientation: int):
    """A 40x20 image whose EXIF says it must be turned to be upright."""
    from PIL import Image

    exif = Image.Exif()
    exif[0x0112] = orientation
    Image.new("RGB", (40, 20), (200, 30, 30)).save(path, format=fmt, exif=exif)


def test_a_heic_comes_out_upright_and_as_a_png(tmp_path):
    # pillow-heif applies the HEIF rotation itself; the PNG keeps that result.
    from PIL import Image

    _heic()  # registers the HEIF opener
    path = tmp_path / "foto.heic"
    _oriented(path, "HEIF", 6)
    with converters._ocr_readable_image(str(path)) as readable:
        with Image.open(readable) as image:
            assert image.format == "PNG" and image.size == (40, 20)


def test_a_non_heic_image_is_passed_through_untouched(tmp_path):
    path = tmp_path / "scan.jpg"
    path.write_bytes(b"not even an image")  # never opened
    with converters._ocr_readable_image(str(path)) as readable:
        assert readable == str(path)
