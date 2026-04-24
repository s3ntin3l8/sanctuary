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
