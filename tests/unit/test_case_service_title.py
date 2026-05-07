"""Tests for auto-case-creation title behavior in case_service.

Covers two recurring problems we hit in the live data:
- ib-0005's case ended up titled "Hansen ./. Liu -" with a trailing-dash
  artifact from `_derive_case_title_from_subject` parsing the email subject
  "8372/25 - Hansen ./. Liu - wg. elterl. Sorge" and stripping at " wg. ".
- The AI metadata stage extracted clean titles like "Hansen ./. Liu
  (elterliche Sorge)" but they were never used: existing draft cases were
  returned as-is, never refreshed.
"""

import pytest

from app.models.database import Case
from app.models.enums import CaseStatus, Jurisdiction
from app.services.case_service import (
    _is_better_title,
    get_or_create_case_from_reference,
)


@pytest.mark.unit
def test_is_better_title_replaces_trailing_dash_artifact():
    assert _is_better_title(
        "Hansen ./. Liu (elterliche Sorge)", "Hansen ./. Liu -", "8372-25"
    )


@pytest.mark.unit
def test_is_better_title_replaces_neuer_fall_fallback():
    assert _is_better_title(
        "Schmidt ./. Schmidt (Sorgerecht)", "Neuer Fall 0042-26", "0042-26"
    )


@pytest.mark.unit
def test_is_better_title_replaces_when_current_empty():
    assert _is_better_title("Anything", None, "0001-25")
    assert _is_better_title("Anything", "", "0001-25")


@pytest.mark.unit
def test_is_better_title_keeps_richer_existing_title():
    """When the current title already has a matter qualifier and the new one
    doesn't, don't replace — avoids losing user edits."""
    assert not _is_better_title(
        "Hansen ./. Liu",
        "Hansen ./. Liu (elterliche Sorge)",
        "8372-25",
    )


@pytest.mark.unit
def test_is_better_title_replaces_with_richer_title():
    """Significantly longer + parenthesized matter qualifier wins."""
    assert _is_better_title(
        "Hansen ./. Liu (elterliche Sorge - § 1671 BGB)",
        "Hansen ./. Liu",
        "8372-25",
    )


@pytest.mark.unit
def test_is_better_title_skips_when_identical():
    assert not _is_better_title(
        "Hansen ./. Liu (Sorgerecht)",
        "Hansen ./. Liu (Sorgerecht)",
        "8372-25",
    )


@pytest.mark.unit
def test_create_uses_ai_case_title_over_email_subject(db_session):
    """On creation, the AI's clean title beats `_derive_case_title_from_subject`,
    which leaves trailing-dash artifacts on subjects like 'X - wg. Y'."""
    case, _, created = get_or_create_case_from_reference(
        db_session,
        internal_id="9999-99",
        batch_subject="9999/99 - Hansen ./. Liu - wg. elterl. Sorge",
        ai_case_title="Hansen ./. Liu (elterliche Sorge)",
        is_draft=True,
    )
    db_session.commit()
    assert created
    assert case.title == "Hansen ./. Liu (elterliche Sorge)"


@pytest.mark.unit
def test_create_falls_back_to_email_subject_when_no_ai_title(db_session):
    case, _, created = get_or_create_case_from_reference(
        db_session,
        internal_id="9998-99",
        batch_subject="Some subject vor dem AG Hamburg",
        ai_case_title=None,
        is_draft=True,
    )
    db_session.commit()
    assert created
    assert case.title == "Some subject"


@pytest.mark.unit
def test_create_falls_back_to_neuer_fall_when_nothing_useful(db_session):
    case, _, created = get_or_create_case_from_reference(
        db_session,
        internal_id="9997-99",
        batch_subject=None,
        ai_case_title=None,
    )
    db_session.commit()
    assert created
    assert case.title == "Neuer Fall 9997-99"


@pytest.mark.unit
def test_existing_draft_case_title_refreshed_on_retry(db_session):
    """A draft case with a trailing-dash title gets upgraded when a fresh
    AI extraction provides a cleaner one — the user's central complaint
    on ib-0005."""
    existing = Case(
        id="9996-99",
        title="Hansen ./. Liu -",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        is_draft=True,
    )
    db_session.add(existing)
    db_session.commit()

    case, _, created = get_or_create_case_from_reference(
        db_session,
        internal_id="9996-99",
        ai_case_title="Hansen ./. Liu (elterliche Sorge)",
    )
    db_session.commit()
    db_session.refresh(case)
    assert not created
    assert case.title == "Hansen ./. Liu (elterliche Sorge)"


@pytest.mark.unit
def test_ratified_case_title_locked(db_session):
    """Once user ratifies the case (is_draft=False), title is preserved
    even on retry — manual edits survive."""
    ratified = Case(
        id="9995-99",
        title="Manually edited title — leave alone",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        is_draft=False,
    )
    db_session.add(ratified)
    db_session.commit()

    case, _, _ = get_or_create_case_from_reference(
        db_session,
        internal_id="9995-99",
        ai_case_title="Some new AI title",
    )
    db_session.commit()
    db_session.refresh(case)
    assert case.title == "Manually edited title — leave alone"
