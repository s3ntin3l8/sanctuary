"""Tests for Phase 4 relationship detector — including hallucination guard."""

from datetime import datetime
from unittest.mock import patch

import pytest

from app.models.database import Document, DocumentRelationship, Proceeding
from app.models.enums import (
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
)


@pytest.fixture
def proceeding_with_docs(db_session, sample_case):
    proceeding = Proceeding(
        case_id=sample_case.id,
        court_name="Amtsgericht Hamburg",
        court_level=ProceedingCourtLevel.AG,
        az_court="003 F 426/25",
        status=ProceedingStatus.ACTIVE,
        ingest_date=datetime.now(),
    )
    db_session.add(proceeding)
    db_session.flush()

    prior1 = Document(
        title="Klageschrift",
        content="Die Klage wird erhoben...",
        case_id=sample_case.id,
        proceeding_id=proceeding.id,
        significance_tier=SignificanceTier.CRITICAL,
        originator_type=OriginatorType.OPPOSING,
        received_date=datetime(2025, 1, 10),
    )
    prior2 = Document(
        title="Beschluss",
        content="Das Gericht beschließt...",
        case_id=sample_case.id,
        proceeding_id=proceeding.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.COURT,
        received_date=datetime(2025, 2, 15),
    )
    new_doc = Document(
        title="Klageerwiderung",
        content="Die Beklagte widerspricht...",
        case_id=sample_case.id,
        proceeding_id=proceeding.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        received_date=datetime(2025, 3, 1),
    )
    db_session.add_all([prior1, prior2, new_doc])
    db_session.commit()
    db_session.refresh(prior1)
    db_session.refresh(prior2)
    db_session.refresh(new_doc)
    return proceeding, prior1, prior2, new_doc


@pytest.mark.unit
def test_relationships_created(db_session, proceeding_with_docs):
    proceeding, prior1, prior2, new_doc = proceeding_with_docs

    ai_result = {
        "relationships": [
            {
                "to_document_id": prior1.id,
                "relationship_type": "replies_to",
                "confidence": "high",
                "notes": "Directly responds to the complaint",
            }
        ]
    }

    with (
        patch(
            "app.services.intelligence.relationship_detector.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector._call_relationship_detector_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.relationship_detector import detect

        detect(new_doc.id)

    rels = (
        db_session.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == new_doc.id)
        .all()
    )
    assert len(rels) == 1
    assert rels[0].to_document_id == prior1.id
    assert rels[0].relationship_type == RelationshipType.REPLIES_TO
    assert rels[0].confidence == RelationshipConfidence.AI_DETECTED


@pytest.mark.unit
def test_ai_detected_edge_does_not_close_target_thread(
    db_session, proceeding_with_docs
):
    """AI-detected REPLIES_TO edges must NOT close the target's thread — only USER_CONFIRMED
    edges count. The user must explicitly confirm before the thread resolves."""
    from app.models.enums import DocumentType

    proceeding, prior1, _prior2, new_doc = proceeding_with_docs
    prior1.document_type = DocumentType.STATEMENT
    prior1.thread_open = True
    db_session.commit()

    ai_result = {
        "relationships": [
            {
                "to_document_id": prior1.id,
                "relationship_type": "replies_to",
                "confidence": "high",
                "notes": "Reply",
            }
        ]
    }

    with (
        patch(
            "app.services.intelligence.relationship_detector.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector._call_relationship_detector_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.relationship_detector import detect

        detect(new_doc.id)

    db_session.expire_all()
    assert db_session.get(Document, prior1.id).thread_open is True


@pytest.mark.unit
def test_hallucination_guard_drops_invalid_id(db_session, proceeding_with_docs):
    """AI returns an ID not in candidate list → relationship must be dropped."""
    proceeding, prior1, prior2, new_doc = proceeding_with_docs

    ai_result = {
        "relationships": [
            {
                "to_document_id": 99999,  # hallucinated ID
                "relationship_type": "replies_to",
                "confidence": "high",
                "notes": "Invented",
            },
            {
                "to_document_id": prior2.id,  # valid
                "relationship_type": "references",
                "confidence": "medium",
                "notes": "Cites the ruling",
            },
        ]
    }

    with (
        patch(
            "app.services.intelligence.relationship_detector.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector._call_relationship_detector_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.relationship_detector import detect

        detect(new_doc.id)

    rels = (
        db_session.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == new_doc.id)
        .all()
    )
    assert len(rels) == 1
    assert rels[0].to_document_id == prior2.id


@pytest.mark.unit
def test_hallucination_guard_drops_invalid_relationship_type(
    db_session, proceeding_with_docs
):
    """AI returns an invalid relationship_type → must be dropped."""
    proceeding, prior1, prior2, new_doc = proceeding_with_docs

    ai_result = {
        "relationships": [
            {
                "to_document_id": prior1.id,
                "relationship_type": "invalidtype",
                "confidence": "high",
                "notes": "test",
            },
        ]
    }

    with (
        patch(
            "app.services.intelligence.relationship_detector.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector._call_relationship_detector_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.relationship_detector import detect

        detect(new_doc.id)

    rels = (
        db_session.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == new_doc.id)
        .all()
    )
    assert len(rels) == 0


@pytest.mark.unit
def test_skips_low_significance_doc(db_session, sample_case):
    """Docs with informational/administrative tier should not trigger relationship detection."""
    doc = Document(
        title="Admin letter",
        content="Acknowledgement of receipt.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.ADMINISTRATIVE,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    with (
        patch(
            "app.services.intelligence.relationship_detector.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector._call_relationship_detector_sync"
        ) as mock_call,
    ):
        from app.services.intelligence.relationship_detector import detect

        detect(doc.id)

        mock_call.assert_not_called()


def _make_prior(db_session, sample_case, title, tier=SignificanceTier.CRITICAL):
    doc = Document(
        title=title,
        content=f"{title} body",
        case_id=sample_case.id,
        significance_tier=tier,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


@pytest.mark.unit
def test_get_prior_docs_blends_semantic_candidate(db_session, sample_case, monkeypatch):
    """A1: a semantically-relevant prior doc OUTSIDE the recency window must be
    surfaced, displacing a lower-ranked recent doc to honour the reserved slot."""
    from app.services.intelligence import relationship_detector as rd

    # Three prior critical docs (a < b < c by id) + the new doc.
    a = _make_prior(db_session, sample_case, "Oldest – semantic target")
    b = _make_prior(db_session, sample_case, "Middle")
    c = _make_prior(db_session, sample_case, "Newest")
    new_doc = _make_prior(db_session, sample_case, "Brand new")
    db_session.commit()

    # Tight budget: 2 total, 1 reserved for semantic → recency keeps only the
    # single newest, semantic contributes the out-of-window old doc.
    target_id = a.id
    monkeypatch.setattr(rd, "MAX_CANDIDATES", 2)
    monkeypatch.setattr(rd, "_SEMANTIC_SLOTS", 1)
    monkeypatch.setattr(rd, "nearest_document_ids", lambda *args, **kw: [target_id])

    result_ids = {d.id for d in rd._get_prior_docs(new_doc, db_session)}

    assert a.id in result_ids, "semantic candidate outside recency window missing"
    assert c.id in result_ids, "most-recent doc should still lead"
    assert b.id not in result_ids, "middle recent doc displaced by reserved slot"
    assert len(result_ids) == 2


@pytest.mark.unit
def test_get_prior_docs_recency_only_when_embeddings_unavailable(
    db_session, sample_case, monkeypatch
):
    """A1 regression: empty KNN (embed failure / cold index) → pure recency,
    identical to pre-A1 behaviour."""
    from app.services.intelligence import relationship_detector as rd

    a = _make_prior(db_session, sample_case, "Oldest")
    b = _make_prior(db_session, sample_case, "Middle")
    c = _make_prior(db_session, sample_case, "Newest")
    new_doc = _make_prior(db_session, sample_case, "Brand new")
    db_session.commit()

    monkeypatch.setattr(rd, "MAX_CANDIDATES", 2)
    monkeypatch.setattr(rd, "nearest_document_ids", lambda *a, **k: [])

    result = rd._get_prior_docs(new_doc, db_session)
    assert [d.id for d in result] == [c.id, b.id]  # id desc, capped at 2
    assert a.id not in {d.id for d in result}


@pytest.mark.unit
def test_get_prior_docs_dedups_semantic_already_in_recency(
    db_session, sample_case, monkeypatch
):
    """A1: KNN ids already covered by recency add nothing (no duplicates)."""
    from app.services.intelligence import relationship_detector as rd

    a = _make_prior(db_session, sample_case, "Oldest")
    b = _make_prior(db_session, sample_case, "Middle")
    c = _make_prior(db_session, sample_case, "Newest")
    new_doc = _make_prior(db_session, sample_case, "Brand new")
    db_session.commit()

    monkeypatch.setattr(rd, "MAX_CANDIDATES", 3)
    monkeypatch.setattr(rd, "nearest_document_ids", lambda *a, **k: [b.id, c.id])

    result = rd._get_prior_docs(new_doc, db_session)
    ids = [d.id for d in result]
    assert sorted(ids) == sorted([a.id, b.id, c.id])
    assert len(ids) == len(set(ids))  # no dup


@pytest.mark.unit
def test_attaches_as_proof_edge_persisted(db_session, proceeding_with_docs):
    """A2: the detector must persist an attaches_as_proof edge (no date gate)."""
    proceeding, prior1, prior2, new_doc = proceeding_with_docs

    ai_result = {
        "relationships": [
            {
                "to_document_id": prior1.id,
                "relationship_type": "attaches_as_proof",
                "confidence": "high",
                "notes": "Tenders the complaint as exhibit",
            }
        ]
    }

    with (
        patch(
            "app.services.intelligence.relationship_detector.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector._call_relationship_detector_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.relationship_detector import detect

        detect(new_doc.id)

    rels = (
        db_session.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == new_doc.id)
        .all()
    )
    assert len(rels) == 1
    assert rels[0].relationship_type == RelationshipType.ATTACHES_AS_PROOF
    assert rels[0].confidence == RelationshipConfidence.AI_DETECTED


@pytest.mark.unit
def test_detects_relationships_across_proceedings(db_session, sample_case):
    # Proceeding 1: AG (lower court)
    p1 = Proceeding(
        case_id=sample_case.id,
        court_name="Amtsgericht Hamburg",
        court_level=ProceedingCourtLevel.AG,
        az_court="001 F 1/25",
        status=ProceedingStatus.CLOSED,
        ingest_date=datetime.now(),
    )
    # Proceeding 2: OLG (appeal court)
    p2 = Proceeding(
        case_id=sample_case.id,
        court_name="Oberlandesgericht Hamburg",
        court_level=ProceedingCourtLevel.OLG,
        az_court="1 UF 1/26",
        status=ProceedingStatus.ACTIVE,
        ingest_date=datetime.now(),
    )
    db_session.add_all([p1, p2])
    db_session.flush()

    doc_p1 = Document(
        title="AG Judgment",
        content="Final judgment from AG.",
        case_id=sample_case.id,
        proceeding_id=p1.id,
        significance_tier=SignificanceTier.CRITICAL,
        originator_type=OriginatorType.COURT,
        issued_date=datetime(2025, 12, 31),
    )
    db_session.add(doc_p1)
    db_session.flush()  # Ensure doc_p1 has an ID

    doc_p2 = Document(
        title="Appeal",
        content="Appealing the AG judgment.",
        case_id=sample_case.id,
        proceeding_id=p2.id,
        significance_tier=SignificanceTier.CRITICAL,
        originator_type=OriginatorType.OWN,
        issued_date=datetime(2026, 1, 15),
    )
    db_session.add(doc_p2)
    db_session.commit()
    db_session.refresh(doc_p1)
    db_session.refresh(doc_p2)

    ai_result = {
        "relationships": [
            {
                "to_document_id": doc_p1.id,
                "relationship_type": "replies_to",
                "confidence": "high",
                "notes": "Appeals the AG judgment",
            }
        ]
    }

    with (
        patch(
            "app.services.intelligence.relationship_detector.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector._call_relationship_detector_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.relationship_detector import detect

        detect(doc_p2.id)

    rels = (
        db_session.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == doc_p2.id)
        .all()
    )
    assert len(rels) == 1
    assert rels[0].to_document_id == doc_p1.id
