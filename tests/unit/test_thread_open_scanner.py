"""Tests for Phase 4d thread-open close-out scanner."""

from datetime import datetime

import pytest

from app.models.database import Document, DocumentRelationship
from app.models.enums import (
    DocumentType,
    OriginatorType,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
)


@pytest.fixture
def thread_open_doc(db_session, sample_case):
    doc = Document(
        title="Stellungnahme",
        content="We assert that...",
        case_id=sample_case.id,
        document_type=DocumentType.STATEMENT,
        thread_open=True,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture
def reply_doc(db_session, sample_case):
    doc = Document(
        title="Erwiderung",
        content="In reply...",
        case_id=sample_case.id,
        document_type=DocumentType.MOTION,
        thread_open=True,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OPPOSING,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.mark.unit
def test_closes_thread_when_confirmed_reply_exists(
    db_session, thread_open_doc, reply_doc
):
    rel = DocumentRelationship(
        from_document_id=reply_doc.id,
        to_document_id=thread_open_doc.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.USER_CONFIRMED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()

    from app.services.intelligence.thread_open_scanner import scan_and_close_threads

    updated = scan_and_close_threads(db_session)
    assert updated >= 1

    db_session.expire_all()
    doc = db_session.get(Document, thread_open_doc.id)
    assert doc.thread_open is False


@pytest.mark.unit
def test_does_not_close_thread_without_reply(db_session, thread_open_doc):
    from app.services.intelligence.thread_open_scanner import scan_and_close_threads

    updated = scan_and_close_threads(db_session)
    assert updated == 0

    db_session.expire_all()
    doc = db_session.get(Document, thread_open_doc.id)
    assert doc.thread_open is True


@pytest.mark.unit
def test_closes_thread_on_references_edge(db_session, thread_open_doc, reply_doc):
    rel = DocumentRelationship(
        from_document_id=reply_doc.id,
        to_document_id=thread_open_doc.id,
        relationship_type=RelationshipType.REFERENCES,
        confidence=RelationshipConfidence.USER_CONFIRMED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()

    from app.services.intelligence.thread_open_scanner import scan_and_close_threads

    updated = scan_and_close_threads(db_session)
    assert updated >= 1

    db_session.expire_all()
    doc = db_session.get(Document, thread_open_doc.id)
    assert doc.thread_open is False


@pytest.mark.unit
def test_reopens_thread_when_confirmed_edge_removed(
    db_session, thread_open_doc, reply_doc
):
    """If a confirmed edge is later removed, the next scan reopens the thread."""
    rel = DocumentRelationship(
        from_document_id=reply_doc.id,
        to_document_id=thread_open_doc.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.USER_CONFIRMED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()

    from app.services.intelligence.thread_open_scanner import scan_and_close_threads

    scan_and_close_threads(db_session)
    db_session.expire_all()
    assert db_session.get(Document, thread_open_doc.id).thread_open is False

    db_session.delete(rel)
    db_session.commit()

    scan_and_close_threads(db_session)
    db_session.expire_all()
    assert db_session.get(Document, thread_open_doc.id).thread_open is True


@pytest.mark.unit
def test_ai_detected_edge_does_not_close_via_scanner(
    db_session, thread_open_doc, reply_doc
):
    """AI_DETECTED edges are ignored by the scanner — thread stays open until user confirms."""
    rel = DocumentRelationship(
        from_document_id=reply_doc.id,
        to_document_id=thread_open_doc.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()

    from app.services.intelligence.thread_open_scanner import scan_and_close_threads

    updated = scan_and_close_threads(db_session)
    assert updated == 0

    db_session.expire_all()
    assert db_session.get(Document, thread_open_doc.id).thread_open is True


@pytest.mark.unit
def test_only_affects_thread_open_docs(
    db_session, sample_case, thread_open_doc, reply_doc
):
    """reply_doc has thread_open=True but is the source of the relationship, not the target."""
    rel = DocumentRelationship(
        from_document_id=reply_doc.id,
        to_document_id=thread_open_doc.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.USER_CONFIRMED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()

    from app.services.intelligence.thread_open_scanner import scan_and_close_threads

    scan_and_close_threads(db_session)

    db_session.expire_all()
    # reply_doc is the source, not a target of any edge — its thread_open should stay True
    source = db_session.get(Document, reply_doc.id)
    assert source.thread_open is True


@pytest.mark.unit
def test_recompute_thread_open_closes_on_confirm(
    db_session, thread_open_doc, reply_doc
):
    """recompute_thread_open closes thread when a USER_CONFIRMED edge exists."""
    rel = DocumentRelationship(
        from_document_id=reply_doc.id,
        to_document_id=thread_open_doc.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.USER_CONFIRMED,
        ingest_date=datetime.now(),
    )
    db_session.add(rel)
    db_session.commit()

    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    result = recompute_thread_open(thread_open_doc.id, db_session)
    assert result is False
    db_session.expire_all()
    assert db_session.get(Document, thread_open_doc.id).thread_open is False


@pytest.mark.unit
def test_recompute_thread_open_reopens_on_reject(
    db_session, thread_open_doc, reply_doc
):
    """recompute_thread_open reopens thread after the last USER_CONFIRMED edge is removed."""
    thread_open_doc.thread_open = False
    db_session.commit()

    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    result = recompute_thread_open(thread_open_doc.id, db_session)
    assert result is True
    db_session.expire_all()
    assert db_session.get(Document, thread_open_doc.id).thread_open is True
