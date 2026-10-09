"""Unit tests for slicer.py's proposed-cuts combine logic.

Covers the whole-batch-AI-failure-vs-per-candidate-failure symmetry bug: both
failure modes must suppress the cut, not just one of them.
"""

from app.services.ingestion.slicer import (
    _combine_proposed_cuts,
    _conservative_ai_failure,
)

_CANDIDATES = [
    (2, "prev tail 1", "curr head 1"),
    (5, "prev tail 2", "curr head 2"),
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
    cuts = _combine_proposed_cuts([(99, "a", "b")], {}, page_count=10)
    assert cuts == []


def test_missing_ai_entry_defaults_to_propose():
    """A candidate with no ai_results entry at all defaults to proposing the
    cut — callers are responsible for pre-filling a conservative entry on
    whole-batch failure; this function's default itself stays `True` so a
    caller that legitimately has no AI opinion isn't silently suppressed."""
    cuts = _combine_proposed_cuts(_CANDIDATES[:1], {}, page_count=10)
    assert cuts == [
        {"page": 2, "confidence": "medium", "kind": "attachment", "notes": ""}
    ]


def test_whole_batch_ai_failure_conservative_fill_suppresses_all_cuts():
    """Regression: a whole-batch AI failure must fail exactly like a
    per-candidate failure (no cuts proposed), not fall through to the
    missing-entry default (which proposes every candidate)."""
    conservative_fill = {
        cut_page: _conservative_ai_failure("boom") for cut_page, _, _ in _CANDIDATES
    }
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
