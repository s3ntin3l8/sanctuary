"""Zip-based documents are refused before they are unpacked when their
central directory declares a decompression bomb."""

import io
import zipfile

import pytest

from app import config
from app.services.ingestion import converters
from app.services.ingestion.archive_guard import ArchiveRejected, check_zip

pytestmark = pytest.mark.unit


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_an_ordinary_document_archive_passes():
    check_zip(_zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w/>" * 50}))


def test_a_highly_compressed_large_member_is_refused():
    bomb = _zip({"word/document.xml": b"\0" * (3 * 1024 * 1024)})
    with pytest.raises(ArchiveRejected, match="compression ratio"):
        check_zip(bomb)


def test_a_small_member_may_compress_well():
    check_zip(_zip({"word/document.xml": b"\0" * 100_000}))


def test_too_many_entries_are_refused(monkeypatch):
    monkeypatch.setattr(config, "MAX_ARCHIVE_ENTRIES", 2)
    with pytest.raises(ArchiveRejected, match="too many entries"):
        check_zip(_zip({"a": b"1", "b": b"2", "c": b"3"}))


def test_a_large_declared_total_is_refused(monkeypatch):
    monkeypatch.setattr(config, "MAX_ARCHIVE_UNCOMPRESSED_MB", 1)
    noisy = bytes(range(256)) * 6000  # ~1.5 MB, barely compressible
    with pytest.raises(ArchiveRejected, match="expands to more than"):
        check_zip(_zip({"a": noisy}))


def test_something_that_is_not_a_zip_is_refused():
    with pytest.raises(ArchiveRejected, match="not a readable archive"):
        check_zip(b"definitely not a zip")


def test_conversion_refuses_a_bomb_before_docling(tmp_path):
    path = tmp_path / "bomb.docx"
    path.write_bytes(_zip({"word/document.xml": b"\0" * (3 * 1024 * 1024)}))
    with pytest.raises(ArchiveRejected):
        converters.convert_file(str(path))
