"""A rejected edge is remembered and never written again (issue #235)."""

from unittest.mock import patch

import pytest

from app.models.database import (
    Document,
    DocumentRelationship,
    IngestBatch,
    RejectedRelationship,
)
from app.models.enums import (
    IngestBatchSourceType,
    OriginatorType,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
)
from app.repositories.document_relationship import (
    insert_edge_if_absent,
    is_rejected,
    reject_edge,
)


@pytest.fixture
def two_docs(db_session, sample_case):
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
    db_session.add(batch)
    db_session.commit()
    docs = [
        Document(
            title=t,
            content="x",
            ingest_batch_id=batch.id,
            case_id=sample_case.id,
            significance_tier=SignificanceTier.SIGNIFICANT,
            originator_type=OriginatorType.OWN,
        )
        for t in ("older", "newer")
    ]
    db_session.add_all(docs)
    db_session.commit()
    return docs  # older, newer


def _edge(db, older, newer, rel_type=RelationshipType.REPLIES_TO):
    assert insert_edge_if_absent(
        db,
        from_document_id=newer.id,
        to_document_id=older.id,
        relationship_type=rel_type,
    )
    db.commit()
    return db.query(DocumentRelationship).one()


@pytest.mark.unit
def test_rejected_edge_is_not_reinserted(db_session, two_docs):
    older, newer = two_docs
    rel = _edge(db_session, older, newer)

    reject_edge(db_session, rel)
    db_session.commit()

    assert db_session.query(DocumentRelationship).count() == 0
    assert is_rejected(
        db_session,
        from_document_id=newer.id,
        to_document_id=older.id,
        relationship_type=RelationshipType.REPLIES_TO,
    )
    assert not insert_edge_if_absent(
        db_session,
        from_document_id=newer.id,
        to_document_id=older.id,
        relationship_type=RelationshipType.REPLIES_TO,
    )
    assert db_session.query(DocumentRelationship).count() == 0


@pytest.mark.unit
def test_rejection_is_per_relationship_type(db_session, two_docs):
    older, newer = two_docs
    reject_edge(db_session, _edge(db_session, older, newer))
    db_session.commit()

    assert insert_edge_if_absent(
        db_session,
        from_document_id=newer.id,
        to_document_id=older.id,
        relationship_type=RelationshipType.REFERENCES,
    )


@pytest.mark.unit
def test_rejecting_twice_is_harmless(db_session, two_docs):
    older, newer = two_docs
    reject_edge(db_session, _edge(db_session, older, newer))
    db_session.commit()
    # Same triple rejected again (e.g. an edge re-created by a user path).
    db_session.add(
        DocumentRelationship(
            from_document_id=newer.id,
            to_document_id=older.id,
            relationship_type=RelationshipType.REPLIES_TO,
            confidence=RelationshipConfidence.USER_CREATED,
        )
    )
    db_session.commit()
    reject_edge(db_session, db_session.query(DocumentRelationship).one())
    db_session.commit()

    assert db_session.query(RejectedRelationship).count() == 1


@pytest.mark.unit
def test_rejection_dies_with_either_document(db_session, two_docs):
    older, newer = two_docs
    reject_edge(db_session, _edge(db_session, older, newer))
    db_session.commit()
    assert db_session.query(RejectedRelationship).count() == 1

    db_session.delete(db_session.get(Document, older.id))
    db_session.commit()

    assert db_session.query(RejectedRelationship).count() == 0


@pytest.mark.unit
def test_ai_detector_does_not_resurrect_a_rejected_edge(db_session, two_docs):
    older, newer = two_docs
    reject_edge(db_session, _edge(db_session, older, newer))
    db_session.commit()

    ai_result = {
        "relationships": [
            {
                "to_document_id": older.id,
                "relationship_type": "replies_to",
                "confidence": "high",
                "notes": "again",
            }
        ]
    }
    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector._call_relationship_detector_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.relationship_detector import detect

        detect(newer.id)

    assert db_session.query(DocumentRelationship).count() == 0
