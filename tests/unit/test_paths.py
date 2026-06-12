"""Unit tests for storage-path helpers (app/core/paths.py)."""

from pathlib import Path

import pytest

from app.core.paths import resolve_storage_path, to_storage_path


@pytest.mark.unit
def test_to_storage_path_absolute_under_data_dir(isolate_data_dir):
    """An absolute path under DATA_DIR is stored relative to DATA_DIR."""
    abs_path = isolate_data_dir / "_TRIAGE" / "1_letter.pdf"
    assert to_storage_path(abs_path) == "_TRIAGE/1_letter.pdf"


@pytest.mark.unit
def test_to_storage_path_already_relative_is_unchanged():
    """A relative path is returned verbatim."""
    assert to_storage_path("_TRIAGE/1_letter.pdf") == "_TRIAGE/1_letter.pdf"


@pytest.mark.unit
def test_to_storage_path_absolute_outside_data_dir_is_unchanged():
    """An absolute path outside DATA_DIR is returned verbatim (caller's concern)."""
    assert to_storage_path("/etc/passwd") == "/etc/passwd"


@pytest.mark.unit
def test_resolve_storage_path_relative_is_rooted_at_data_dir(isolate_data_dir):
    """A relative stored path resolves to DATA_DIR / path."""
    resolved = resolve_storage_path("_TRIAGE/1_letter.pdf")
    assert resolved == isolate_data_dir / "_TRIAGE" / "1_letter.pdf"
    assert resolved.is_absolute()


@pytest.mark.unit
def test_resolve_storage_path_absolute_is_unchanged():
    """An absolute stored path is returned unchanged."""
    assert resolve_storage_path("/abs/path/file.pdf") == Path("/abs/path/file.pdf")


@pytest.mark.unit
def test_roundtrip_to_then_resolve(isolate_data_dir):
    """to_storage_path then resolve_storage_path recovers the original absolute path."""
    abs_path = isolate_data_dir / "ADV-024-A" / "doc.pdf"
    assert resolve_storage_path(to_storage_path(abs_path)) == abs_path
