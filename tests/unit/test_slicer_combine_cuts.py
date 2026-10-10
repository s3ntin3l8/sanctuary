"""Unit tests for slicer.py's proposed-cuts combine logic.

Covers the whole-batch-AI-failure-vs-per-candidate-failure symmetry bug: both
failure modes must suppress the cut, not just one of them.
"""

from app.services.ingestion.slicer import (
    _apply_outline,
    _Boundary,
    _boundary_signals,
    _combine_proposed_cuts,
    _conservative_ai_failure,
    _outline_parts,
    _outline_prompt,
)

_CANDIDATES = [
    _Boundary(2, "prev tail 1", "curr head 1"),
    _Boundary(5, "prev tail 2", "curr head 2"),
]


def test_ai_agrees_proposes_cut():
    ai_results = {2: {"is_new_document": True}}
    cuts = _combine_proposed_cuts(_CANDIDATES[:1], ai_results, page_count=10)
    assert cuts == [
        {"page": 2, "confidence": "medium", "kind": "attachment", "notes": ""}
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


def test_the_models_own_confidence_is_ignored():
    for claimed in ("high", "low", "certain"):
        ai_results = {2: {"is_new_document": True, "confidence": claimed}}
        cuts = _combine_proposed_cuts(_CANDIDATES[:1], ai_results, page_count=10)
        assert cuts[0]["confidence"] == "medium", claimed


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
    assert result == {"is_new_document": False, "failed": True, "notes": "timeout"}


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


def _cuts_for(*signals, ai=None):
    cand = _Boundary(2, "tail", "head", signals=tuple(signals))
    ai = ai or {"is_new_document": True}
    return _combine_proposed_cuts([cand], {2: ai}, page_count=10)


def test_a_start_signal_makes_the_cut_high():
    for signal in (
        "page_reset",
        "az_change",
        "salutation_signature",
        "enclosure_marker",
    ):
        assert _cuts_for(signal)[0]["confidence"] == "high", signal


def test_a_repeated_header_with_the_same_date_is_low():
    assert _cuts_for("repeated_header")[0]["confidence"] == "low"


def test_a_repeated_court_letterhead_with_a_new_date_is_a_real_cut():
    """Two Verfügungen in a row share the letterhead; the date tells them apart."""
    cut = _cuts_for("repeated_header", "date_line_change")[0]
    assert cut["confidence"] == "medium"


def test_a_start_cue_outranks_a_repeated_header():
    assert _cuts_for("repeated_header", "az_change")[0]["confidence"] == "high"


def test_ai_no_with_a_start_signal_is_proposed_low_with_the_disagreement():
    cut = _cuts_for("page_reset", ai={"is_new_document": False, "notes": "carries on"})[
        0
    ]
    assert cut["confidence"] == "low"
    assert cut["notes"] == "AI: continuation — carries on (signals: page_reset)"


def test_ai_no_without_a_start_signal_proposes_nothing():
    assert _cuts_for(ai={"is_new_document": False}) == []
    assert (
        _cuts_for("blank_page", "date_line_change", ai={"is_new_document": False}) == []
    )


def test_a_failed_judgment_never_proposes_even_with_signals():
    assert _cuts_for("page_reset", ai=_conservative_ai_failure("boom")) == []


def test_repeated_header_signal_fires_on_a_clinic_report_second_page():
    head = (
        "Klinikum Ingolstadt - Arztbrief vom 21.07.2026 (erzeugt am 22.07.2026) Seite"
    )
    assert "repeated_header" in _boundary_signals(head + " 1", "tail", head + " 2")


def test_repeated_header_ignores_different_letterheads_and_short_pages():
    assert "repeated_header" not in _boundary_signals(
        "Amtsgericht Ingolstadt - Familiengericht Beschluss",
        "t",
        "Rechtsanwälte Funk Haidl und Partner Schriftsatz",
    )
    assert "repeated_header" not in _boundary_signals("Seite 1", "t", "Seite 2")


def _cut(page, kind="letter", confidence="high", notes=""):
    return {"page": page, "confidence": confidence, "kind": kind, "notes": notes}


def test_outline_parts_follow_the_cuts_that_start_cut():
    parts = _outline_parts([_cut(3), _cut(6, confidence="low"), _cut(8)], page_count=10)
    assert [(p.number, p.first, p.last) for p in parts] == [
        (1, 1, 2),
        (2, 3, 7),
        (3, 8, 10),
    ]


def test_outline_prompt_shares_one_budget_and_gives_up_on_too_many_parts():
    pages = [{"text_head": "H" * 900, "text_tail": "T" * 900} for _ in range(30)]
    parts = _outline_parts([_cut(p) for p in range(2, 28)], page_count=30)
    prompt = _outline_prompt(parts, pages)
    assert prompt is not None and len(prompt) < 20000
    assert "Part 27 (pages 27-30)" in prompt
    many = [_cut(p) for p in range(2, 200)]
    assert _outline_prompt(_outline_parts(many, 200), pages * 7) is None


def test_outline_flips_a_forwarded_part_to_attachment():
    cuts = [_cut(3, "letter"), _cut(6, "letter")]
    parts = _outline_parts(cuts, page_count=8)
    outline = {
        "parts": [
            {"part": 2, "kind": "attachment", "notes": "forwarded by the court"},
            {"part": 3, "kind": "letter", "notes": "new matter"},
        ]
    }
    result = _apply_outline(cuts, parts, outline, frozenset())
    assert [c["kind"] for c in result] == ["attachment", "letter"]
    assert result[0]["notes"] == "outline: forwarded by the court"
    assert result[1] is cuts[1]  # unchanged


def test_outline_cannot_turn_a_marker_page_into_a_letter():
    cuts = [_cut(3, "attachment")]
    parts = _outline_parts(cuts, page_count=5)
    outline = {"parts": [{"part": 2, "kind": "letter", "notes": "x"}]}
    assert _apply_outline(cuts, parts, outline, frozenset({3})) == cuts


def test_malformed_outline_changes_nothing():
    cuts = [_cut(3, "letter")]
    parts = _outline_parts(cuts, page_count=5)
    for bad in (
        {},
        [],
        {"parts": "x"},
        {"parts": [{"part": "2", "kind": "attachment"}]},
    ):
        assert _apply_outline(cuts, parts, bad, frozenset()) == cuts


def test_low_proposals_keep_their_judged_kind():
    cuts = [_cut(3, "letter", confidence="low"), _cut(6, "letter")]
    parts = _outline_parts(cuts, page_count=8)
    outline = {"parts": [{"part": 2, "kind": "attachment", "notes": "n"}]}
    result = _apply_outline(cuts, parts, outline, frozenset())
    assert [c["kind"] for c in result] == ["letter", "attachment"]
