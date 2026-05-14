"""Tests for Phase 5a case brief generator."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.models.enums import CaseStatus, ProceedingStatus
from app.services.intelligence.case_brief_generator import (
    _apply_brief,
    _compute_parties,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_doc(attributed_originator=None, originator_type="court"):
    doc = MagicMock()
    doc.attributed_originator = attributed_originator
    doc.originator_type = originator_type
    return doc


def _make_case(status=CaseStatus.INTAKE):
    case = MagicMock()
    case.id = "ADV-001-A"
    case.status = status
    case.ai_brief = None
    case.ai_brief_updated_at = None
    return case


# ---------------------------------------------------------------------------
# _apply_brief tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_brief_happy_path():
    """Given a valid result dict, sets ai_brief and ai_brief_updated_at."""
    case = _make_case()
    db = MagicMock()
    result = {
        "posture": "Claimant has initiative; discovery phase.",
        "pressure_points": ["Statute of limitations", "Missing evidence"],
        "next_move": "File response by 2026-05-01.",
        "detected_status": "discovery",
        "status_rationale": "Pre-pleading exchange ongoing.",
    }

    _apply_brief(case, result, db)

    assert case.ai_brief["posture"] == "Claimant has initiative; discovery phase."
    assert case.ai_brief["pressure_points"] == [
        "Statute of limitations",
        "Missing evidence",
    ]
    assert case.ai_brief["next_move"] == "File response by 2026-05-01."
    assert isinstance(case.ai_brief_updated_at, datetime)


@pytest.mark.unit
def test_apply_brief_empty_posture_stored_as_empty_string():
    """Empty posture string is stored as '' (not None)."""
    case = _make_case()
    db = MagicMock()
    result = {
        "posture": "",
        "pressure_points": [],
        "next_move": "Wait.",
        "detected_status": "intake",
        "status_rationale": "No documents ingested yet.",
    }

    _apply_brief(case, result, db)

    assert case.ai_brief["posture"] == ""


@pytest.mark.unit
def test_apply_brief_non_string_pressure_points_filtered():
    """Non-string items in pressure_points are removed."""
    case = _make_case()
    db = MagicMock()
    result = {
        "posture": "ok",
        "pressure_points": [123, "real", None, "also real"],
        "next_move": "act",
        "detected_status": "intake",
        "status_rationale": "Only administrative correspondence.",
    }

    _apply_brief(case, result, db)

    assert case.ai_brief["pressure_points"] == ["real", "also real"]


@pytest.mark.unit
def test_apply_brief_updates_case_status_when_detected_differs():
    """A valid detected_status that differs from case.status overwrites it."""
    case = _make_case(status=CaseStatus.INTAKE)
    db = MagicMock()
    result = {
        "posture": "",
        "pressure_points": [],
        "next_move": "",
        "detected_status": "pre_trial",
        "status_rationale": "Klage filed 2026-02-10.",
    }

    _apply_brief(case, result, db)

    assert case.status == CaseStatus.PRE_TRIAL
    assert case.ai_brief["detected_status"] == "pre_trial"
    assert case.ai_brief["status_rationale"] == "Klage filed 2026-02-10."


@pytest.mark.unit
def test_apply_brief_closed_cascades_to_proceedings():
    """detected_status=closed updates case.status and cascade-closes proceedings."""
    case = _make_case(status=CaseStatus.POST_TRIAL)
    db = MagicMock()
    result = {
        "posture": "",
        "pressure_points": [],
        "next_move": "",
        "detected_status": "closed",
        "status_rationale": "All appeals exhausted; no open action items.",
    }

    _apply_brief(case, result, db)

    assert case.status == CaseStatus.CLOSED
    db.query.return_value.filter.return_value.update.assert_called_once_with(
        {"status": ProceedingStatus.CLOSED}
    )


@pytest.mark.unit
def test_apply_brief_invalid_detected_status_keeps_current_status():
    """Garbage detected_status doesn't crash and keeps the existing case.status."""
    case = _make_case(status=CaseStatus.TRIAL)
    db = MagicMock()
    result = {
        "posture": "",
        "pressure_points": [],
        "next_move": "",
        "detected_status": "not_a_real_stage",
        "status_rationale": "—",
    }

    _apply_brief(case, result, db)

    assert case.status == CaseStatus.TRIAL
    db.query.assert_not_called()


# ---------------------------------------------------------------------------
# _compute_parties tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_parties_aggregation():
    """Two court docs and one opposing doc produce 2 entries sorted by count desc."""
    docs = [
        _make_doc(attributed_originator="Court", originator_type="court"),
        _make_doc(attributed_originator="Court", originator_type="court"),
        _make_doc(attributed_originator="Opposing", originator_type="opposing"),
    ]

    parties = _compute_parties(docs)

    assert len(parties) == 2
    assert parties[0]["name"] == "Court"
    assert parties[0]["document_count"] == 2
    assert parties[0]["role"] == "court"
    assert parties[1]["name"] == "Opposing"
    assert parties[1]["document_count"] == 1
    assert parties[1]["role"] == "opposing"


@pytest.mark.unit
def test_compute_parties_skip_null_originator():
    """Documents with attributed_originator=None are excluded."""
    docs = [
        _make_doc(attributed_originator=None, originator_type="unknown"),
        _make_doc(attributed_originator="Court", originator_type="court"),
    ]

    parties = _compute_parties(docs)

    assert len(parties) == 1
    assert parties[0]["name"] == "Court"


@pytest.mark.unit
def test_compute_parties_empty_list():
    """Empty document list returns empty parties list."""
    result = _compute_parties([])
    assert result == []
