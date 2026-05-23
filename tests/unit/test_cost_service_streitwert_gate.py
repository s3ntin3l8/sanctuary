"""ib-0033 #98 regression: Streitwert (Verfahrenswert) is only authoritative
when set BY a court IN a court document. An opposing-party letter (or any
non-court source) may quote a prior ruling's Streitwert, but quoting is not
ruling — the AI must not write that number to a CostSignal.

The previous behaviour materialised STREITWERT signals on any cost_delta the
AI emitted with kind="streitwert", regardless of source document type. Doc
#98 (originator=opposing) wrote a 5000-EUR Streitwert signal because the
opposing party quoted a prior court ruling in their submission.
"""

from datetime import datetime

import pytest

from app.models.database import CostSignal, Document
from app.models.enums import (
    CostSignalType,
    OriginatorType,
    SignificanceTier,
)
from app.services.cost_service import (
    materialize_cost_signal,
    purge_disqualified_streitwert,
)


def _make_doc(
    db,
    case,
    *,
    originator_type: OriginatorType,
    court_relay: bool = False,
) -> Document:
    doc = Document(
        title="Test doc",
        content="content",
        case_id=case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=originator_type,
        court_relay=court_relay,
        issued_date=datetime(2025, 5, 1),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.mark.unit
def test_court_document_creates_streitwert_signal(db_session, sample_case):
    """Direct court document (originator=COURT, court_relay=False) is the
    authoritative source. The signal materialises normally."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)
    result = materialize_cost_signal(
        doc,
        {"kind": "streitwert", "amount": 5000.0, "description": "Streitwertbeschluss"},
        db_session,
    )
    db_session.commit()

    assert isinstance(result, CostSignal)
    assert result.signal_type == CostSignalType.STREITWERT
    assert result.amount == 5000.0


@pytest.mark.unit
def test_opposing_document_blocked_from_creating_streitwert(db_session, sample_case):
    """ib-0033 #98 case: an opposing-party letter quotes a prior ruling's
    Streitwert. The gate must block the signal — opposing parties don't set
    Streitwert, courts do."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.OPPOSING)
    result = materialize_cost_signal(
        doc,
        {"kind": "streitwert", "amount": 5000.0},
        db_session,
    )
    db_session.commit()

    assert result is None
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 0
    )


@pytest.mark.unit
def test_court_relay_blocked_from_creating_streitwert(db_session, sample_case):
    """A court relay (Begleitschreiben forwarding a party submission) has
    originator_type=COURT but court_relay=True — the inner author is a
    party, not the court. Streitwert from such a doc is not authoritative."""
    doc = _make_doc(
        db_session,
        sample_case,
        originator_type=OriginatorType.COURT,
        court_relay=True,
    )
    result = materialize_cost_signal(
        doc,
        {"kind": "streitwert", "amount": 5000.0},
        db_session,
    )
    db_session.commit()

    assert result is None


@pytest.mark.unit
def test_originator_flip_erases_stale_streitwert_on_reenrichment(
    db_session, sample_case
):
    """Direct ib-0033 #98 regression: doc was originally classified
    originator=COURT and a STREITWERT signal was created. Re-enrichment
    flipped originator to OPPOSING. The next materialize call (during retry)
    must erase the stale signal — not just refuse to create a new one."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)

    # First run: doc was treated as COURT and a signal was created.
    materialize_cost_signal(
        doc,
        {"kind": "streitwert", "amount": 5000.0},
        db_session,
    )
    db_session.commit()
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 1
    )

    # Re-enrichment: originator flipped to OPPOSING.
    doc.originator_type = OriginatorType.OPPOSING
    db_session.commit()

    result = materialize_cost_signal(
        doc,
        {"kind": "streitwert", "amount": 5000.0},
        db_session,
    )
    db_session.commit()

    assert result is None
    # Stale signal must be erased on the flip — blocking re-creation alone
    # would leave the bad row in place.
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 0
    )


@pytest.mark.unit
def test_non_streitwert_signals_unaffected_by_gate(db_session, sample_case):
    """The gate is specific to STREITWERT. Other CostSignal kinds
    (cost_ruling, pkh_grant, pkh_denied) materialise unchanged from any
    source — these gates, if any, are out of scope for this fix."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.OPPOSING)
    result = materialize_cost_signal(
        doc,
        {"kind": "pkh_grant", "description": "PKH bewilligt"},
        db_session,
    )
    db_session.commit()

    assert isinstance(result, CostSignal)
    assert result.signal_type == CostSignalType.PKH_GRANT


# --- Fix 5b: Ordnungsgeld and other false-positive descriptions ---------------


@pytest.mark.unit
def test_ordnungsgeld_description_blocked_even_on_court_doc(db_session, sample_case):
    """Round 2 regression (docs #97, #95): a court document emitting a
    cost_delta whose description quotes the § 33 FamFG Ordnungsgeld ceiling
    ("Ordnungsgeld bis zu 1.000,00 €") must be rejected by the false-positive
    guard — Ordnungsgeld is a penalty maximum, not a Verfahrenswert."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)
    result = materialize_cost_signal(
        doc,
        {
            "kind": "streitwert",
            "amount": 1000.0,
            "description": (
                "Das einzelne Ordnungsgeld kann bis zu 1.000,00 € betragen "
                "(§ 33 FamFG i.V.m. Art. 6 Abs. 1 EGStGB)"
            ),
        },
        db_session,
    )
    db_session.commit()

    assert result is None
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 0
    )


@pytest.mark.unit
def test_clean_streitwertbeschluss_still_accepted(db_session, sample_case):
    """The false-positive guard must not over-fire: a legitimate Streitwert-
    festsetzung description (no Ordnungsgeld/bis-zu wording) still creates
    a signal on a court document."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)
    result = materialize_cost_signal(
        doc,
        {
            "kind": "streitwert",
            "amount": 5000.0,
            "description": (
                "Der Verfahrenswert wird auf 5.000 EUR festgesetzt "
                "(Streitwertbeschluss)"
            ),
        },
        db_session,
    )
    db_session.commit()

    assert isinstance(result, CostSignal)
    assert result.amount == 5000.0


@pytest.mark.unit
def test_ordnungsgeld_description_erases_stale_signal(db_session, sample_case):
    """On the false-positive description path (court doc with Ordnungsgeld
    text), the gate must ALSO erase any prior stale row — same pattern as
    the originator gate, so re-enrichment cleans up regardless of which
    rule trips."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)
    # Seed a stale signal from a prior run.
    materialize_cost_signal(
        doc,
        {"kind": "streitwert", "amount": 5000.0, "description": "Streitwertbeschluss"},
        db_session,
    )
    db_session.commit()
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 1
    )

    # New run: AI returns an Ordnungsgeld false-positive.
    result = materialize_cost_signal(
        doc,
        {
            "kind": "streitwert",
            "amount": 1000.0,
            "description": "Ordnungsgeld bis zu 1.000,00 € (§ 33 FamFG)",
        },
        db_session,
    )
    db_session.commit()

    assert result is None
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 0
    )


# --- Fix 6: Proactive cleanup when AI emits no cost_delta --------------------


@pytest.mark.unit
def test_proactive_purge_erases_stale_when_originator_flipped(db_session, sample_case):
    """Round 2 doc #98 case: a STREITWERT signal exists from a prior court
    classification. The AI on retry correctly emits NO cost_delta. The
    proactive purge must erase the stale row — `_ensure_cost_signal` is never
    called in this scenario, so the in-gate erase doesn't fire."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)
    materialize_cost_signal(
        doc,
        {"kind": "streitwert", "amount": 5000.0, "description": "Streitwertbeschluss"},
        db_session,
    )
    db_session.commit()
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 1
    )

    # Originator flips on re-enrichment (AI now correctly identifies as opposing).
    doc.originator_type = OriginatorType.OPPOSING
    db_session.commit()

    deleted = purge_disqualified_streitwert(doc, db_session)
    db_session.commit()

    assert deleted == 1
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 0
    )


@pytest.mark.unit
def test_proactive_purge_no_op_on_court_doc(db_session, sample_case):
    """When the doc still qualifies as a court source, the proactive purge
    must NOT touch existing Streitwert signals."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)
    materialize_cost_signal(
        doc,
        {"kind": "streitwert", "amount": 5000.0, "description": "Streitwertbeschluss"},
        db_session,
    )
    db_session.commit()

    deleted = purge_disqualified_streitwert(doc, db_session)
    db_session.commit()

    assert deleted == 0
    assert (
        db_session.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .count()
        == 1
    )
