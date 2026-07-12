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
    _normalize_case_title,
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
    # Canonical form: dash separator, no parens around matter.
    assert case.title == "Hansen ./. Liu - elterliche Sorge"


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
    assert case.title == "Hansen ./. Liu - elterliche Sorge"


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


# ---------------------------------------------------------------------------
# _normalize_case_title — drift-to-canonical conversion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normalize_paren_matter_to_dash_matter():
    assert (
        _normalize_case_title("Hansen ./. Liu (Umgangsrecht)")
        == "Hansen ./. Liu - Umgangsrecht"
    )
    assert (
        _normalize_case_title("Liu, Yingying ./. Hansen, Björn (Trennungsunterhalt)")
        == "Liu, Yingying ./. Hansen, Björn - Trennungsunterhalt"
    )


@pytest.mark.unit
def test_normalize_preserves_eA_when_converting_paren_matter():
    """A paren-matter title that ALSO has (eA) keeps the eA suffix in canonical form."""
    assert (
        _normalize_case_title("Hansen ./. Liu (Umgangsrecht) (eA)")
        == "Hansen ./. Liu - Umgangsrecht (eA)"
    )


@pytest.mark.unit
def test_normalize_comma_eA_suffix():
    assert _normalize_case_title("Kindesunterhalt, eA") == "Kindesunterhalt (eA)"
    assert (
        _normalize_case_title("Hansen ./. Liu - Umgangsrecht, eA")
        == "Hansen ./. Liu - Umgangsrecht (eA)"
    )


@pytest.mark.unit
def test_normalize_dash_eA_suffix():
    """' - eA' at the end (would clash with the dash separator) becomes ' (eA)'."""
    assert (
        _normalize_case_title("Hansen ./. Liu - Umgangsrecht - eA")
        == "Hansen ./. Liu - Umgangsrecht (eA)"
    )


@pytest.mark.unit
def test_normalize_full_einstweilige_anordnung_to_eA():
    assert (
        _normalize_case_title("Hansen ./. Liu - Umgangsrecht einstweilige Anordnung")
        == "Hansen ./. Liu - Umgangsrecht (eA)"
    )
    assert (
        _normalize_case_title("Kindesunterhalt - einstweilige Anordnung")
        == "Kindesunterhalt (eA)"
    )


@pytest.mark.unit
def test_normalize_strips_internal_id_prefix():
    assert (
        _normalize_case_title("8372/25 - Hansen ./. Liu - Sorgerecht")
        == "Hansen ./. Liu - Sorgerecht"
    )
    assert (
        _normalize_case_title("8372-25: Hansen ./. Liu - Sorgerecht")
        == "Hansen ./. Liu - Sorgerecht"
    )


@pytest.mark.unit
def test_normalize_strips_trailing_punctuation():
    assert _normalize_case_title("Hansen ./. Liu -") == "Hansen ./. Liu"
    assert (
        _normalize_case_title("Hansen ./. Liu - Sorgerecht,")
        == "Hansen ./. Liu - Sorgerecht"
    )
    assert (
        _normalize_case_title("Hansen ./. Liu - Sorgerecht  ")
        == "Hansen ./. Liu - Sorgerecht"
    )


@pytest.mark.unit
def test_normalize_idempotent_on_canonical_titles():
    """Already-canonical titles must round-trip unchanged."""
    for canonical in [
        "Hansen ./. Liu - Sorgerecht",
        "Hansen ./. Liu - Umgangsrecht (eA)",
        "Kindesunterhalt",
        "Kindesunterhalt (eA)",
        "Sorgerecht - Hansen",
    ]:
        assert _normalize_case_title(canonical) == canonical


@pytest.mark.unit
def test_normalize_handles_none_and_empty():
    assert _normalize_case_title(None) is None
    assert _normalize_case_title("") is None
    assert _normalize_case_title("   ") is None


@pytest.mark.unit
def test_normalize_caps_at_120_chars_preserving_eA():
    long_matter = "x" * 200
    out = _normalize_case_title(f"Hansen ./. Liu - {long_matter} (eA)")
    assert out is not None
    assert len(out) <= 120
    assert out.endswith(" (eA)")


# ---------------------------------------------------------------------------
# _is_better_title — eA preference + dash-vs-paren style preference
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_better_title_indifferent_to_format_only_changes():
    """When new and current normalize to the same canonical form (legacy paren
    vs dash style with same content), `_is_better_title` returns False."""
    assert not _is_better_title(
        "Hansen ./. Liu - Sorgerecht",
        "Hansen ./. Liu (Sorgerecht)",
        "8372-25",
    )


@pytest.mark.unit
def test_is_better_title_eA_marker_wins_over_no_marker():
    assert _is_better_title(
        "Hansen ./. Liu - Umgangsrecht (eA)",
        "Hansen ./. Liu - Umgangsrecht",
        "9194-26",
    )


@pytest.mark.unit
def test_is_better_title_does_not_drop_eA():
    """If current has (eA) and new doesn't, that's a regression — don't apply."""
    assert not _is_better_title(
        "Hansen ./. Liu - Umgangsrecht",
        "Hansen ./. Liu - Umgangsrecht (eA)",
        "9194-26",
    )


# ---------------------------------------------------------------------------
# End-to-end: get_or_create_case_from_reference applies normalization
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_normalizes_paren_style_ai_title_to_dash(db_session):
    """An AI title in old paren style gets normalized to canonical dash style at creation."""
    case, _, _ = get_or_create_case_from_reference(
        db_session,
        internal_id="9990-99",
        ai_case_title="Hansen ./. Liu (Sorgerecht)",
        is_draft=True,
    )
    db_session.commit()
    assert case.title == "Hansen ./. Liu - Sorgerecht"


@pytest.mark.unit
def test_create_normalizes_eA_variants_at_creation(db_session):
    case, _, _ = get_or_create_case_from_reference(
        db_session,
        internal_id="9989-99",
        ai_case_title="Kindesunterhalt, eA",
        is_draft=True,
    )
    db_session.commit()
    assert case.title == "Kindesunterhalt (eA)"


@pytest.mark.unit
def test_existing_draft_gets_eA_marker_added(db_session):
    """Draft case with no eA marker gets refreshed when AI now identifies it as eA."""
    existing = Case(
        id="9987-99",
        title="Hansen ./. Liu - Umgangsrecht",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        is_draft=True,
    )
    db_session.add(existing)
    db_session.commit()

    case, _, _ = get_or_create_case_from_reference(
        db_session,
        internal_id="9987-99",
        ai_case_title="Hansen ./. Liu - Umgangsrecht (eA)",
    )
    db_session.commit()
    db_session.refresh(case)
    assert case.title == "Hansen ./. Liu - Umgangsrecht (eA)"
