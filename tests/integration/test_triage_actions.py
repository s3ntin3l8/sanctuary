"""Integration tests for triage document actions."""

from datetime import datetime
from unittest.mock import patch

import pytest

from app.models.database import Document, DocumentRelationship
from app.models.enums import (
    DocumentType,
    OriginatorType,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
)


@pytest.mark.integration
def test_retry_ai_action(app_client, db_session):
    # 1. Setup a doc in failed state
    doc = Document(
        title="Failed AI Doc",
        ai_summary={"error": "Ollama offline"},
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    # 2. Mock process_document_task.delay
    with patch(
        "app.tasks.document_processing.process_document_task.delay"
    ) as mock_delay:
        # 3. Call the retry-ai endpoint
        response = app_client.post(f"/triage/document/{doc.id}/retry-ai")

        assert response.status_code == 200
        # Check if the task was queued
        mock_delay.assert_called_once_with(doc.id)


@pytest.mark.integration
def test_confirm_relationship_closes_thread(app_client, db_session, sample_case):
    """Confirming an AI-detected REPLIES_TO edge immediately closes the target's thread."""
    parent = Document(
        title="Stellungnahme",
        content="The statement...",
        case_id=sample_case.id,
        document_type=DocumentType.STATEMENT,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        thread_open=True,
    )
    reply = Document(
        title="Erwiderung",
        content="In reply...",
        case_id=sample_case.id,
        document_type=DocumentType.MOTION,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OPPOSING,
        thread_open=True,
    )
    db_session.add_all([parent, reply])
    db_session.commit()
    db_session.refresh(parent)
    db_session.refresh(reply)

    rel = DocumentRelationship(
        from_document_id=reply.id,
        to_document_id=parent.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()
    db_session.refresh(rel)

    assert parent.thread_open is True  # not closed yet — AI_DETECTED doesn't count

    response = app_client.post(f"/triage/relationship/{rel.id}/confirm")
    assert response.status_code == 200

    db_session.expire_all()
    assert db_session.get(Document, parent.id).thread_open is False


@pytest.mark.integration
def test_reject_relationship_keeps_thread_open(app_client, db_session, sample_case):
    """Rejecting an AI-detected REPLIES_TO edge leaves the target's thread open."""
    parent = Document(
        title="Stellungnahme",
        content="The statement...",
        case_id=sample_case.id,
        document_type=DocumentType.STATEMENT,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        thread_open=True,
    )
    reply = Document(
        title="Erwiderung",
        content="In reply...",
        case_id=sample_case.id,
        document_type=DocumentType.MOTION,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OPPOSING,
        thread_open=True,
    )
    db_session.add_all([parent, reply])
    db_session.commit()
    db_session.refresh(parent)
    db_session.refresh(reply)

    rel = DocumentRelationship(
        from_document_id=reply.id,
        to_document_id=parent.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()
    db_session.refresh(rel)

    response = app_client.delete(f"/triage/relationship/{rel.id}")
    assert response.status_code == 200

    db_session.expire_all()
    assert db_session.get(Document, parent.id).thread_open is True


@pytest.mark.integration
def test_confirm_relationship_clears_source_unresolved_reason(
    app_client, db_session, sample_case
):
    """Confirming a relationship removes unresolved_relationship from source doc's review_reasons."""
    parent = Document(
        title="Stellungnahme",
        content="The statement...",
        case_id=sample_case.id,
        document_type=DocumentType.STATEMENT,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        thread_open=True,
    )
    source = Document(
        title="Erwiderung",
        content="In reply...",
        case_id=sample_case.id,
        document_type=DocumentType.MOTION,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OPPOSING,
        review_reasons=["unresolved_relationship"],
        needs_review=True,
    )
    db_session.add_all([parent, source])
    db_session.commit()
    db_session.refresh(parent)
    db_session.refresh(source)

    rel = DocumentRelationship(
        from_document_id=source.id,
        to_document_id=parent.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()
    db_session.refresh(rel)

    response = app_client.post(f"/triage/relationship/{rel.id}/confirm")
    assert response.status_code == 200

    db_session.expire_all()
    refreshed = db_session.get(Document, source.id)
    assert "unresolved_relationship" not in (refreshed.review_reasons or [])


@pytest.mark.integration
def test_reject_relationship_clears_source_unresolved_reason(
    app_client, db_session, sample_case
):
    """Rejecting a relationship removes unresolved_relationship from source doc's review_reasons."""
    parent = Document(
        title="Stellungnahme",
        content="The statement...",
        case_id=sample_case.id,
        document_type=DocumentType.STATEMENT,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        thread_open=True,
    )
    source = Document(
        title="Erwiderung",
        content="In reply...",
        case_id=sample_case.id,
        document_type=DocumentType.MOTION,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OPPOSING,
        review_reasons=["unresolved_relationship"],
        needs_review=True,
    )
    db_session.add_all([parent, source])
    db_session.commit()
    db_session.refresh(parent)
    db_session.refresh(source)

    rel = DocumentRelationship(
        from_document_id=source.id,
        to_document_id=parent.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()
    db_session.refresh(rel)

    response = app_client.delete(f"/triage/relationship/{rel.id}")
    assert response.status_code == 200

    db_session.expire_all()
    refreshed = db_session.get(Document, source.id)
    assert "unresolved_relationship" not in (refreshed.review_reasons or [])


@pytest.mark.integration
def test_confirm_relationship_keeps_unresolved_when_second_ai_edge_remains(
    app_client, db_session, sample_case
):
    """When a second AI-detected edge still exists after confirm, unresolved_relationship stays."""
    parent1 = Document(
        title="Stellungnahme 1",
        content="First statement...",
        case_id=sample_case.id,
        document_type=DocumentType.STATEMENT,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        thread_open=True,
    )
    parent2 = Document(
        title="Stellungnahme 2",
        content="Second statement...",
        case_id=sample_case.id,
        document_type=DocumentType.STATEMENT,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        thread_open=True,
    )
    source = Document(
        title="Erwiderung",
        content="In reply to both...",
        case_id=sample_case.id,
        document_type=DocumentType.MOTION,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OPPOSING,
        review_reasons=["unresolved_relationship"],
        needs_review=True,
    )
    db_session.add_all([parent1, parent2, source])
    db_session.commit()
    db_session.refresh(parent1)
    db_session.refresh(parent2)
    db_session.refresh(source)

    rel1 = DocumentRelationship(
        from_document_id=source.id,
        to_document_id=parent1.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
        ingest_date=datetime.now(),
    )
    rel2 = DocumentRelationship(
        from_document_id=source.id,
        to_document_id=parent2.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
        ingest_date=datetime.now(),
    )
    db_session.add_all([rel1, rel2])
    db_session.commit()
    db_session.refresh(rel1)

    response = app_client.post(f"/triage/relationship/{rel1.id}/confirm")
    assert response.status_code == 200

    db_session.expire_all()
    refreshed = db_session.get(Document, source.id)
    # rel2 is still AI_DETECTED → unresolved_relationship must persist
    assert "unresolved_relationship" in (refreshed.review_reasons or [])
    assert refreshed.needs_review is True
