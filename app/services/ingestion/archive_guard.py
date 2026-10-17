"""Refuse a decompression bomb before Docling unpacks a zip-based document."""

import io
import zipfile
from pathlib import Path

from app import config

ARCHIVE_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
# Small members compress extremely well legitimately (repetitive XML); only a
# member that also expands to something large counts against the ratio.
_RATIO_FLOOR_BYTES = 1024 * 1024


class ArchiveRejected(ValueError):
    """The archive's declared contents exceed what we are willing to unpack."""


def check_zip(source: bytes | str | Path) -> None:
    """Raise ``ArchiveRejected`` when the central directory declares too much.

    Reads only the directory, never a member. ``zipfile`` itself stops a member
    at its declared size, so the declared numbers are what bounds extraction.
    """
    try:
        archive = zipfile.ZipFile(
            io.BytesIO(source) if isinstance(source, bytes) else source
        )
    except zipfile.BadZipFile as exc:
        raise ArchiveRejected("File is not a readable archive") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > config.MAX_ARCHIVE_ENTRIES:
            raise ArchiveRejected(
                f"Archive has too many entries ({len(entries)} > {config.MAX_ARCHIVE_ENTRIES})"
            )
        total = sum(entry.file_size for entry in entries)
        if total > config.MAX_ARCHIVE_UNCOMPRESSED_MB * 1024 * 1024:
            raise ArchiveRejected(
                f"Archive expands to more than {config.MAX_ARCHIVE_UNCOMPRESSED_MB} MB"
            )
        for entry in entries:
            if (
                entry.file_size > _RATIO_FLOOR_BYTES
                and entry.file_size
                > config.MAX_ARCHIVE_RATIO * max(entry.compress_size, 1)
            ):
                raise ArchiveRejected(
                    f"Archive member {entry.filename!r} has a suspicious compression ratio"
                )
