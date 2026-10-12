"""Locate a document's stored original file.

Shared by the REST download route and the MCP ``get_original_file`` tool so the
DATA_DIR containment check lives in one place.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from app.models.database import Document


class OriginalFileError(Exception):
    """The original cannot be served; ``code``/``detail`` match the REST error."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class OriginalFile:
    path: Path
    filename: str

    @property
    def media_type(self) -> str:
        guessed, _ = mimetypes.guess_type(self.filename)
        return guessed or "application/octet-stream"


def resolve_original(doc: Document) -> OriginalFile:
    """The existing stored file for ``doc``, guaranteed to sit inside DATA_DIR."""
    from app.config import DATA_DIR
    from app.core.paths import resolve_storage_path

    if not doc.file_path:
        raise OriginalFileError(
            "no_original", "No original file stored for this document."
        )
    resolved = resolve_storage_path(doc.file_path).resolve()
    data_root = DATA_DIR.resolve()
    # Refuse anything outside DATA_DIR even if file_path were ever attacker-influenced.
    if not str(resolved).startswith(str(data_root) + "/") or not resolved.is_file():
        raise OriginalFileError("no_original", "Original file not found on disk.")
    return OriginalFile(path=resolved, filename=resolved.name)
