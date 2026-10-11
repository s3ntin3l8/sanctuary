"""Unit tests for `reconcile_ai_fields`.

The reconciler resolves four AI self-contradictions:
- R1: is_court_document=false ∧ originator_type=court  → originator_type=unknown
- R2: court_relay=true ∧ sender is not a court name → court_relay=false
- R3: doc_type ∈ {MOTION, STATEMENT} ∧ originator_type=court → originator_type=unknown
- R4: doc_type=relay ∧ party originator → doc_type=correspondence

These rules fire only when the AI's own output internally contradicts itself
(or contradicts the system prompt's explicit rules). They are not judgment
overrides — they pick the high-confidence field and clear the low-confidence
contradicting one.
"""

from datetime import datetime

import pytest

from app.models.database import Document
from app.models.enums import DocumentType, OriginatorType, SignificanceTier
from app.services.intelligence._court_identity import reconcile_ai_fields


def _make_doc(
    db,
    case,
    *,
    sender: str | None = None,
    document_type: DocumentType | None = None,
    originator_type: OriginatorType = OriginatorType.UNKNOWN,
) -> Document:
    doc = Document(
        title="Reconciler test doc",
        content="content",
        case_id=case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=originator_type,
        document_type=document_type,
        sender=sender,
        issued_date=datetime(2026, 5, 25),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.mark.unit
def test_r1_not_court_doc_but_court_originator(db_session, sample_case):
    """Metadata stage: AI says is_court_document=false but emits originator_type=court.
    The reconciler clears originator_type to 'unknown'."""
    doc = _make_doc(db_session, sample_case, sender="Some Lawyer")
    ai_output = {
        "is_court_document": False,
        "originator_type": "court",
        "sender": "Some Lawyer",
    }

    fired = reconcile_ai_fields(doc, ai_output)

    assert "R1_not_court_doc_but_court_originator" in fired
    assert ai_output["originator_type"] == "unknown"


@pytest.mark.unit
def test_r2_court_relay_with_lawyer_sender(db_session, sample_case):
    """Enricher stage: court_relay=true but sender is a law firm.
    The reconciler flips court_relay to False."""
    doc = _make_doc(db_session, sample_case, sender="Haidl Funk Rechtsanwälte")
    ai_output = {"court_relay": True}

    fired = reconcile_ai_fields(doc, ai_output)

    assert "R2_court_relay_but_non_court_sender" in fired
    assert ai_output["court_relay"] is False


@pytest.mark.unit
def test_r2_court_relay_with_court_sender_passes(db_session, sample_case):
    """Counter-test: court_relay=true with a real court sender is allowed."""
    doc = _make_doc(db_session, sample_case, sender="Amtsgericht Ingolstadt")
    ai_output = {"court_relay": True}

    fired = reconcile_ai_fields(doc, ai_output)

    assert fired == []
    assert ai_output["court_relay"] is True


@pytest.mark.unit
def test_r3_motion_with_court_originator_metadata_stage(db_session, sample_case):
    """Metadata stage: AI emits document_type=motion + originator_type=court.
    The reconciler clears originator_type to 'unknown' (mutates ai_output)."""
    doc = _make_doc(db_session, sample_case, sender="Haidl Funk Rechtsanwälte")
    ai_output = {"document_type": "motion", "originator_type": "court"}

    fired = reconcile_ai_fields(doc, ai_output)

    assert "R3_party_authored_type_but_court_originator" in fired
    assert ai_output["originator_type"] == "unknown"
    # Doc not mutated because the metadata-stage path mutates the dict.
    # (The metadata apply layer reads ai_output['originator_type'] and writes the doc.)


@pytest.mark.unit
def test_r3_motion_with_court_originator_enricher_stage(db_session, sample_case):
    """Enricher stage: AI just set document_type=motion; doc.originator_type was
    previously COURT (from Phase-1). Enricher AI output has no `originator` key.
    The reconciler must mutate doc.originator_type directly because the
    enricher apply layer does not read 'originator_type' from its result dict."""
    doc = _make_doc(
        db_session,
        sample_case,
        sender="Haidl Funk Rechtsanwälte",
        originator_type=OriginatorType.COURT,
    )
    ai_output = {"document_type": "motion"}  # no 'originator_type' key

    fired = reconcile_ai_fields(doc, ai_output)

    assert "R3_party_authored_type_but_court_originator" in fired
    assert doc.originator_type == OriginatorType.UNKNOWN


@pytest.mark.unit
def test_r3_statement_also_triggers(db_session, sample_case):
    """STATEMENT (Klageerwiderung/Stellungnahme) is also party-authored."""
    doc = _make_doc(db_session, sample_case, sender="Haidl Funk Rechtsanwälte")
    ai_output = {"document_type": "statement", "originator_type": "court"}

    fired = reconcile_ai_fields(doc, ai_output)

    assert "R3_party_authored_type_but_court_originator" in fired
    assert ai_output["originator_type"] == "unknown"


@pytest.mark.unit
def test_r3_ruling_does_not_trigger(db_session, sample_case):
    """Counter-test: RULING (Beschluss/Urteil) IS court-authored — no rule fires."""
    doc = _make_doc(db_session, sample_case, sender="Amtsgericht Ingolstadt")
    ai_output = {"document_type": "ruling", "originator_type": "court"}

    fired = reconcile_ai_fields(doc, ai_output)

    assert fired == []
    assert ai_output["originator_type"] == "court"


@pytest.mark.unit
def test_consistent_ai_output_no_rules_fire(db_session, sample_case):
    """Counter-test: a fully-consistent AI output triggers no reconciliation."""
    doc = _make_doc(db_session, sample_case, sender="Amtsgericht Ingolstadt")
    ai_output = {
        "is_court_document": True,
        "originator_type": "court",
        "sender": "Amtsgericht Ingolstadt",
        "court_relay": False,
        "document_type": "ruling",
    }

    fired = reconcile_ai_fields(doc, ai_output)

    assert fired == []


@pytest.mark.unit
def test_multiple_rules_fire_in_one_pass(db_session, sample_case):
    """Doc-9-like pattern: lawyer sender, AI emits is_court_document=false,
    originator=court, court_relay=true — R1 and R2 both fire."""
    doc = _make_doc(db_session, sample_case, sender="Haidl Funk WMA")
    ai_output = {
        "is_court_document": False,
        "originator_type": "court",
        "sender": "Haidl Funk WMA",
        "court_relay": True,
    }

    fired = reconcile_ai_fields(doc, ai_output)

    assert "R1_not_court_doc_but_court_originator" in fired
    assert "R2_court_relay_but_non_court_sender" in fired
    assert ai_output["originator_type"] == "unknown"
    assert ai_output["court_relay"] is False


@pytest.mark.unit
@pytest.mark.parametrize("originator", ["opposing", "own", "third_party"])
def test_r4_relay_type_with_party_originator(db_session, sample_case, originator):
    """A party letter typed `relay` (first slice of a scanned packet) is
    correspondence; only a court can relay."""
    doc = _make_doc(
        db_session,
        sample_case,
        sender="Haidl Funk Rechtsanwälte",
        originator_type=OriginatorType(originator),
    )
    ai_output = {"document_type": "relay", "court_relay": False}

    fired = reconcile_ai_fields(doc, ai_output)

    assert "R4_relay_type_but_party_originator" in fired
    assert ai_output["document_type"] == "correspondence"


@pytest.mark.unit
def test_r4_court_relay_passes(db_session, sample_case):
    doc = _make_doc(
        db_session,
        sample_case,
        sender="Amtsgericht Ingolstadt",
        originator_type=OriginatorType.COURT,
    )
    ai_output = {"document_type": "relay", "court_relay": True}

    assert reconcile_ai_fields(doc, ai_output) == []
    assert ai_output["document_type"] == "relay"
