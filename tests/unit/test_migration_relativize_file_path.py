"""Unit test for the file_path relativization migration (f62172cfb232)."""

import importlib.util
from pathlib import Path

import pytest

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "f62172cfb232_relativize_document_file_path.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_mig_relativize", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_relativize_strips_host_prefix_up_to_data():
    mig = _load_migration()
    src = "/home/bjoern/projects/sanctuary/data/_TRIAGE/1_Schr.pdf"
    assert mig.relativize(src) == "_TRIAGE/1_Schr.pdf"


@pytest.mark.unit
def test_relativize_works_for_other_host_layout():
    mig = _load_migration()
    assert mig.relativize("/srv/app/data/ADV-024-A/doc.pdf") == "ADV-024-A/doc.pdf"


@pytest.mark.unit
def test_relativize_returns_none_without_data_segment():
    mig = _load_migration()
    assert mig.relativize("/etc/passwd") is None
