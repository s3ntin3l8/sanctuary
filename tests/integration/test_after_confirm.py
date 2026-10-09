"""Review state after confirmation: cover letters, retries, reworded contradictions."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document, DocumentRelationship, IngestBatch, User
from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    RelationshipConfidence,
    RelationshipType,
)
from app.services.ai_summary import _already_acknowledged
from app.services.ingestion.service import refresh_review_reasons
from app.services.triage_retry import reset_batch_for_retry
from app.services.triage_subgroups import set_cover_letter

pytestmark = pytest.mark.integration

client = TestClient(app)


def _owner(db) -> int:
    return db.query(User).filter_by(email="admin@localhost").one().id


def _batch(db, case_id: str) -> IngestBatch:
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.COMPLETED,
        case_id=case_id,
        owner_id=_owner(db),
    )
    db.add(batch)
    db.flush()
    return batch


def _doc(db, batch, case_id, title, **kw) -> Document:
    doc = Document(
        title=title,
        case_id=case_id,
        owner_id=_owner(db),
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
        sender="Gericht",
        received_date=datetime.now(UTC),
        issued_date=datetime.now(UTC),
        confirmed_at=datetime.now(UTC),
        **kw,
    )
    db.add(doc)
    db.flush()
    return doc


def _encloses(db, cover, enclosure):
    db.add(
        DocumentRelationship(
            from_document_id=cover.id,
            to_document_id=enclosure.id,
            relationship_type=RelationshipType.ENCLOSES,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db.flush()


def test_cover_letter_enclosure_edges_do_not_flag_review(db_session, sample_case):
    batch = _batch(db_session, sample_case.id)
    cover = _doc(
        db_session, batch, sample_case.id, "Cover", role=DocumentRole.COVER_LETTER
    )
    anlage = _doc(
        db_session, batch, sample_case.id, "Anlage", role=DocumentRole.ENCLOSURE
    )
    _encloses(db_session, cover, anlage)
    db_session.commit()

    refresh_review_reasons(cover, db_session)
    assert "unresolved_relationship" not in cover.review_reasons
    assert cover.needs_review is False


def test_choosing_another_cover_letter_moves_the_enclosure_edges(
    db_session, sample_case
):
    batch = _batch(db_session, sample_case.id)
    old = _doc(
        db_session, batch, sample_case.id, "Old cover", role=DocumentRole.COVER_LETTER
    )
    new = _doc(
        db_session,
        batch,
        sample_case.id,
        "New cover",
        role=DocumentRole.ENCLOSURE,
        parent_id=old.id,
    )
    _encloses(db_session, old, new)
    db_session.commit()

    set_cover_letter(db_session, new.id, batch.id)
    db_session.commit()

    edges = {
        (r.from_document_id, r.to_document_id)
        for r in db_session.query(DocumentRelationship).filter_by(
            relationship_type=RelationshipType.ENCLOSES
        )
    }
    assert edges == {(new.id, old.id)}
    db_session.refresh(old)
    db_session.refresh(new)
    assert old.parent_id == new.id and old.role == DocumentRole.ENCLOSURE
    assert new.parent_id is None and new.role == DocumentRole.COVER_LETTER


def test_full_retry_puts_the_bundle_back_under_confirmation(db_session, sample_case):
    batch = _batch(db_session, sample_case.id)
    doc = _doc(db_session, batch, sample_case.id, "Doc")
    db_session.commit()
    assert doc.confirmed_at is not None

    assert reset_batch_for_retry(batch, db_session, full=True) != -1
    db_session.commit()

    db_session.refresh(doc)
    assert doc.confirmed_at is None
    refresh_review_reasons(doc, db_session)
    assert "pending_confirmation" in doc.review_reasons


def test_stage_retry_keeps_the_confirmation(db_session, sample_case):
    batch = _batch(db_session, sample_case.id)
    doc = _doc(db_session, batch, sample_case.id, "Doc")
    db_session.commit()

    assert reset_batch_for_retry(batch, db_session, full=False) != -1
    db_session.commit()

    db_session.refresh(doc)
    assert doc.confirmed_at is not None


def test_reworded_contradiction_stays_acknowledged_but_a_new_one_reraises():
    acknowledged = ["The letter says payment was made, but the invoice is still open."]
    reworded = ["The letter says payment was made but the invoice is still open"]
    assert _already_acknowledged(reworded, acknowledged)
    assert not _already_acknowledged(
        [*reworded, "The hearing date differs from the court notice."], acknowledged
    )
    assert not _already_acknowledged(reworded, None)


def test_case_detail_documents_name_their_open_reasons(db_session, sample_case):
    batch = _batch(db_session, sample_case.id)
    doc = _doc(
        db_session, batch, sample_case.id, "Doc", meta={"ai_contradiction": True}
    )
    db_session.commit()
    refresh_review_reasons(doc, db_session)

    body = client.get(f"/api/v1/cases/{sample_case.id}").json()
    (shown,) = [d for d in body["documents"] if d["id"] == doc.id]
    assert shown["open_review_reasons"] == ["contradiction_detected"]


def test_a_rejected_enclosure_edge_stays_rejected_when_the_cover_changes(
    db_session, sample_case
):
    from app.repositories.document_relationship import reject_edge

    batch = _batch(db_session, sample_case.id)
    old = _doc(db_session, batch, sample_case.id, "Old", role=DocumentRole.COVER_LETTER)
    new = _doc(
        db_session,
        batch,
        sample_case.id,
        "New",
        role=DocumentRole.ENCLOSURE,
        parent_id=old.id,
    )
    other = _doc(
        db_session, batch, sample_case.id, "Other", role=DocumentRole.ENCLOSURE
    )
    _encloses(db_session, old, new)
    _encloses(db_session, old, other)
    rejected = (
        db_session.query(DocumentRelationship)
        .filter_by(from_document_id=old.id, to_document_id=other.id)
        .one()
    )
    reject_edge(db_session, rejected)
    # The user also rejected the edge the new cover would get to this enclosure.
    db_session.add(
        DocumentRelationship(
            from_document_id=new.id,
            to_document_id=other.id,
            relationship_type=RelationshipType.ENCLOSES,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db_session.flush()
    again = (
        db_session.query(DocumentRelationship)
        .filter_by(from_document_id=new.id, to_document_id=other.id)
        .one()
    )
    reject_edge(db_session, again)
    db_session.commit()

    set_cover_letter(db_session, new.id, batch.id)
    db_session.commit()

    edges = {
        (r.from_document_id, r.to_document_id)
        for r in db_session.query(DocumentRelationship).filter_by(
            relationship_type=RelationshipType.ENCLOSES
        )
    }
    assert edges == {(new.id, old.id)}  # new→other stays rejected
