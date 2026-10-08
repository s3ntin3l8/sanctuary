"""Source-level guards for two bug classes the ingestion audit hit repeatedly."""

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

# Binds once at import time — possibly during pytest collection, before the
# conftest fixture repoints app.config.SessionLocal at the test database.
# app/dependencies.py is the one place the harness patches directly.
_MODULE_LEVEL_SESSIONLOCAL = re.compile(
    r"^from app\.config import\b.*\bSessionLocal\b", re.MULTILINE
)
# Multi-line `from app.config import (\n ... SessionLocal ...)` form.
_MODULE_LEVEL_SESSIONLOCAL_PAREN = re.compile(
    r"^from app\.config import \([^)]*\bSessionLocal\b", re.MULTILINE
)
# Naive datetime written into a timestamptz column is shifted by the DB
# session's TimeZone GUC; use app.core.timezone.now_utc().
_NAIVE_NOW = re.compile(r"\bdatetime\.now\(\s*\)")


def _py_files():
    return sorted(p for p in APP.rglob("*.py"))


@pytest.mark.unit
def test_no_module_level_sessionlocal_import():
    offenders = [
        str(p.relative_to(APP.parent))
        for p in _py_files()
        if p.relative_to(APP).as_posix() != "dependencies.py"
        and (
            _MODULE_LEVEL_SESSIONLOCAL.search(p.read_text())
            or _MODULE_LEVEL_SESSIONLOCAL_PAREN.search(p.read_text())
        )
    ]
    assert not offenders, (
        "Import SessionLocal inside the function (from app.config import "
        f"SessionLocal), not at module level: {offenders}"
    )


@pytest.mark.unit
def test_no_naive_datetime_now():
    offenders = [
        str(p.relative_to(APP.parent))
        for p in _py_files()
        if _NAIVE_NOW.search(p.read_text())
    ]
    assert not offenders, f"Use app.core.timezone.now_utc(): {offenders}"
