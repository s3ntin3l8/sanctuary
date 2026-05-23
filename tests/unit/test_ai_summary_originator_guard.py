"""Fix 8c: refuse AI metadata override on confirmed court documents.

ib-0039 doc #113 reenactment: even when `case.opposing_parties` is polluted
with court names (via the auto-bootstrap feedback loop), the AI's per-doc
metadata writeback at `ai_summary.enrich_document_with_ai` must not corrupt
a clearly-court document's originator. Defense in depth alongside Fix 8a/8b
which prevent the pollution in the first place.
"""

from datetime import datetime

import pytest

from app.models.database import Document
from app.models.enums import (
    DocumentType,
    OriginatorType,
    SignificanceTier,
)
from app.services.ai_summary import enrich_document_with_ai


def _make_doc(
    db,
    case,
    *,
    document_type: DocumentType,
    originator_type: OriginatorType = OriginatorType.COURT,
    sender: str | None = None,
) -> Document:
    doc = Document(
        title="Test doc",
        content="content",
        case_id=case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=originator_type,
        document_type=document_type,
        sender=sender,
        issued_date=datetime(2025, 5, 1),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.mark.unit
def test_ruling_keeps_court_even_when_ai_says_opposing(db_session, sample_case):
    """ib-0039 #113 case: doc_type=RULING + sender=OLG München. AI's metadata
    extraction says originator='opposing' (because case.opposing_parties is
    polluted). The guard must refuse the override."""
    doc = _make_doc(
        db_session,
        sample_case,
        document_type=DocumentType.RULING,
        originator_type=OriginatorType.COURT,
        sender="Oberlandesgericht München",
    )

    enrich_document_with_ai(doc, {"originator": "opposing"}, db_session)
    db_session.commit()

    assert doc.originator_type == OriginatorType.COURT


@pytest.mark.unit
def test_relay_keeps_court_even_when_ai_says_opposing(db_session, sample_case):
    """A court Begleitschreiben (doc_type=RELAY) is also a confirmed court doc."""
    doc = _make_doc(
        db_session,
        sample_case,
        document_type=DocumentType.RELAY,
        originator_type=OriginatorType.COURT,
        sender="Amtsgericht Ingolstadt",
    )

    enrich_document_with_ai(doc, {"originator": "opposing"}, db_session)
    db_session.commit()

    assert doc.originator_type == OriginatorType.COURT


@pytest.mark.unit
def test_court_named_sender_blocks_party_override(db_session, sample_case):
    """Doc with non-court doc_type (e.g. CORRESPONDENCE) but a court-named
    sender. Sender is still a hard signal — court name → block override."""
    doc = _make_doc(
        db_session,
        sample_case,
        document_type=DocumentType.CORRESPONDENCE,
        originator_type=OriginatorType.COURT,
        sender="Landgericht Berlin",
    )

    enrich_document_with_ai(doc, {"originator": "opposing"}, db_session)
    db_session.commit()

    assert doc.originator_type == OriginatorType.COURT


@pytest.mark.unit
def test_court_doc_accepts_court_originator_update(db_session, sample_case):
    """The guard only blocks party-side overrides. AI confirming 'court' on
    a court doc still applies (idempotent no-op in practice, but the path
    must be unobstructed)."""
    doc = _make_doc(
        db_session,
        sample_case,
        document_type=DocumentType.RULING,
        originator_type=OriginatorType.UNKNOWN,
        sender="Oberlandesgericht München",
    )

    enrich_document_with_ai(doc, {"originator": "court"}, db_session)
    db_session.commit()

    assert doc.originator_type == OriginatorType.COURT


@pytest.mark.unit
def test_non_court_doc_accepts_opposing_override(db_session, sample_case):
    """Counter-test: a non-court doc (MOTION, party sender) MUST still get
    its OPPOSING classification from the AI — the guard must not over-fire
    and break the normal path."""
    doc = _make_doc(
        db_session,
        sample_case,
        document_type=DocumentType.MOTION,
        originator_type=OriginatorType.UNKNOWN,
        sender="Yingying Liu",
    )

    enrich_document_with_ai(doc, {"originator": "opposing"}, db_session)
    db_session.commit()

    assert doc.originator_type == OriginatorType.OPPOSING
