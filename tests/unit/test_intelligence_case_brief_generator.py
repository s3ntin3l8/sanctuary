"""Tests for Phase 5a case brief generator."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

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


def _make_case():
    case = MagicMock()
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
    result = {
        "posture": "Claimant has initiative; discovery phase.",
        "pressure_points": ["Statute of limitations", "Missing evidence"],
        "next_move": "File response by 2026-05-01.",
    }

    _apply_brief(case, result)

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
    result = {
        "posture": "",
        "pressure_points": [],
        "next_move": "Wait.",
    }

    _apply_brief(case, result)

    assert case.ai_brief["posture"] == ""


@pytest.mark.unit
def test_apply_brief_non_string_pressure_points_filtered():
    """Non-string items in pressure_points are removed."""
    case = _make_case()
    result = {
        "posture": "ok",
        "pressure_points": [123, "real", None, "also real"],
        "next_move": "act",
    }

    _apply_brief(case, result)

    assert case.ai_brief["pressure_points"] == ["real", "also real"]


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
