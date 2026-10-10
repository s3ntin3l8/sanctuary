"""Unit tests for slicer.py's proposed-cuts combine logic.

Covers the whole-batch-AI-failure-vs-per-candidate-failure symmetry bug: both
failure modes must suppress the cut, not just one of them.
"""

from app.services.ingestion.slicer import (
    _Boundary,
    _combine_proposed_cuts,
    _conservative_ai_failure,
)

_CANDIDATES = [
    _Boundary(2, "prev tail 1", "curr head 1"),
    _Boundary(5, "prev tail 2", "curr head 2"),
]


def test_ai_agrees_proposes_cut():
    ai_results = {2: {"is_new_document": True, "confidence": "high"}}
    cuts = _combine_proposed_cuts(_CANDIDATES[:1], ai_results, page_count=10)
    assert cuts == [
        {"page": 2, "confidence": "high", "kind": "attachment", "notes": ""}
    ]


def test_ai_disagrees_suppresses_cut():
    ai_results = {2: {"is_new_document": False, "confidence": "high"}}
    cuts = _combine_proposed_cuts(_CANDIDATES[:1], ai_results, page_count=10)
    assert cuts == []


def test_out_of_range_cut_page_dropped():
    cuts = _combine_proposed_cuts([_Boundary(99, "a", "b")], {}, page_count=10)
    assert cuts == []


def test_missing_ai_entry_proposes_nothing():
    """A boundary the AI never answered is not cut: an outage cannot over-cut."""
    assert _combine_proposed_cuts(_CANDIDATES, {}, page_count=10) == []
    assert _combine_proposed_cuts(_CANDIDATES[:1], {2: {}}, page_count=10) == []


def test_unknown_confidence_is_low():
    ai_results = {2: {"is_new_document": True, "confidence": "certain"}}
    cuts = _combine_proposed_cuts(_CANDIDATES[:1], ai_results, page_count=10)
    assert cuts[0]["confidence"] == "low"


def test_whole_batch_ai_failure_conservative_fill_suppresses_all_cuts():
    """A failed judgment per candidate proposes nothing."""
    conservative_fill = {c.page: _conservative_ai_failure("boom") for c in _CANDIDATES}
    cuts = _combine_proposed_cuts(_CANDIDATES, conservative_fill, page_count=10)
    assert cuts == []


def test_ai_agrees_string_truthy_values():
    for truthy in ("true", "True", "1", "yes", "YES"):
        ai_results = {2: {"is_new_document": truthy}}
        cuts = _combine_proposed_cuts(_CANDIDATES[:1], ai_results, page_count=10)
        assert cuts == [
            {"page": 2, "confidence": "medium", "kind": "attachment", "notes": ""}
        ], truthy


def test_conservative_ai_failure_shape():
    result = _conservative_ai_failure("timeout")
    assert result == {
        "is_new_document": False,
        "confidence": "low",
        "notes": "timeout",
    }


def test_kind_letter_is_carried_through():
    ai_results = {2: {"is_new_document": True, "kind": "Letter"}}
    cuts = _combine_proposed_cuts(_CANDIDATES[:1], ai_results, page_count=10)
    assert cuts[0]["kind"] == "letter"


def test_missing_or_unknown_kind_is_an_attachment():
    for ai in ({"is_new_document": True}, {"is_new_document": True, "kind": "memo"}):
        cuts = _combine_proposed_cuts(_CANDIDATES[:1], {2: ai}, page_count=10)
        assert cuts[0]["kind"] == "attachment"


def test_enclosure_marker_forces_attachment():
    ai_results = {2: {"is_new_document": True, "kind": "letter"}}
    cuts = _combine_proposed_cuts(
        _CANDIDATES[:1], ai_results, page_count=10, marker_pages=frozenset({2})
    )
    assert cuts[0]["kind"] == "attachment"
