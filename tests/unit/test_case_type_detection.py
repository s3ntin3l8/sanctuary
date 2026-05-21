"""Tests for automatic case type detection from Aktenzeichen and backfill logic."""

import pytest

from app.models.database import Case, Proceeding
from app.models.enums import (
    CaseStatus,
    CaseType,
    Jurisdiction,
    ProceedingCourtLevel,
    ProceedingStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _case(db, case_id="C-001", case_type=CaseType.CIVIL, assume_worst_case=True):
    c = Case(
        id=case_id,
        title="Test",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        case_type=case_type,
        assume_worst_case=assume_worst_case,
    )
    db.add(c)
    db.flush()
    return c


def _proceeding(db, case_id, az_court=None, level=ProceedingCourtLevel.AG):
    p = Proceeding(
        case_id=case_id,
        court_name="Amtsgericht Hamburg",
        court_level=level,
        status=ProceedingStatus.ACTIVE,
        az_court=az_court,
    )
    db.add(p)
    db.flush()
    return p


# ---------------------------------------------------------------------------
# parse_case_type
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_case_type_valid():
    from app.models.enums import parse_case_type

    assert parse_case_type("family") == CaseType.FAMILY
    assert parse_case_type("civil") == CaseType.CIVIL
    assert parse_case_type("administrative") == CaseType.ADMINISTRATIVE
    assert parse_case_type("criminal") == CaseType.CRIMINAL
    # Case-insensitive
    assert parse_case_type("FAMILY") == CaseType.FAMILY
    assert parse_case_type("  Civil  ") == CaseType.CIVIL


@pytest.mark.unit
def test_parse_case_type_invalid():
    from app.models.enums import parse_case_type

    assert parse_case_type(None) is None
    assert parse_case_type("") is None
    assert parse_case_type("unknown") is None
    assert parse_case_type("Zivilsache") is None


# ---------------------------------------------------------------------------
# _maybe_set_case_type_from_az
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_maybe_set_case_type_from_az_sets_family(db_session):
    from app.services.case_service import _maybe_set_case_type_from_az

    case = _case(db_session)
    assert case.case_type == CaseType.CIVIL
    assert case.assume_worst_case is True

    _maybe_set_case_type_from_az(case, "3 F 426/25")

    assert case.case_type == CaseType.FAMILY
    assert case.assume_worst_case is False  # also flipped


@pytest.mark.unit
def test_maybe_set_case_type_from_az_no_overwrite_non_civil(db_session):
    """Once case_type is non-CIVIL, the helper must not overwrite it."""
    from app.services.case_service import _maybe_set_case_type_from_az

    case = _case(db_session, case_type=CaseType.FAMILY)
    _maybe_set_case_type_from_az(case, "12 O 345/25")  # O = CIVIL
    # Should remain FAMILY — guard blocks overwrite
    assert case.case_type == CaseType.FAMILY


@pytest.mark.unit
def test_maybe_set_case_type_from_az_unknown_code_no_change(db_session):
    from app.services.case_service import _maybe_set_case_type_from_az

    case = _case(db_session)
    _maybe_set_case_type_from_az(case, "22 T 342/26")  # T = unknown
    assert case.case_type == CaseType.CIVIL  # unchanged


@pytest.mark.unit
def test_maybe_set_case_type_assume_worst_case_respected(db_session):
    """When assume_worst_case was already False, the helper must not flip it to True."""
    from app.services.case_service import _maybe_set_case_type_from_az

    case = _case(db_session, assume_worst_case=False)
    _maybe_set_case_type_from_az(case, "3 F 426/25")
    assert case.case_type == CaseType.FAMILY
    assert case.assume_worst_case is False  # stays False, not flipped


# ---------------------------------------------------------------------------
# backfill_case_types
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_backfill_family_proceeding_updates_case(db_session):
    from app.services.case_service import backfill_case_types

    case = _case(db_session, "BF-001")
    _proceeding(db_session, case.id, az_court="3 F 426/25")
    db_session.commit()

    counts = backfill_case_types(db_session)
    db_session.commit()

    db_session.refresh(case)
    assert case.case_type == CaseType.FAMILY
    assert case.assume_worst_case is False
    assert counts["updated"] >= 1


@pytest.mark.unit
def test_backfill_skips_already_classified(db_session):
    from app.services.case_service import backfill_case_types

    case = _case(db_session, "BF-002", case_type=CaseType.CRIMINAL)
    _proceeding(db_session, case.id, az_court="3 F 426/25")
    db_session.commit()

    counts = backfill_case_types(db_session)
    db_session.refresh(case)

    # CRIMINAL stays — guard blocks overwrite
    assert case.case_type == CaseType.CRIMINAL
    assert counts["skipped"] >= 1


@pytest.mark.unit
def test_backfill_unknown_az_no_change(db_session):
    from app.services.case_service import backfill_case_types

    case = _case(db_session, "BF-003")
    _proceeding(db_session, case.id, az_court="22 T 342/26")
    db_session.commit()

    backfill_case_types(db_session)
    db_session.refresh(case)

    assert case.case_type == CaseType.CIVIL  # unchanged
