"""Gmail threadId fallback for the email-header linker (issue #236)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.database import (
    Document,
    DocumentRelationship,
    GmailMessageIndex,
    IngestBatch,
)
from app.models.enums import (
    DocumentType,
    IngestBatchSourceType,
    RelationshipConfidence,
    RelationshipType,
)
from app.services.intelligence.thread_header_linker import link_batch

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _mail(db, case_id, n, *, thread, minutes, in_reply_to=None, docs=True):
    """An ingested email ``n`` minutes after T0, indexed under Gmail ``thread``."""
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        message_id=f"<m{n}@x>",
        in_reply_to=in_reply_to,
        thread_refs=[in_reply_to] if in_reply_to else None,
        received_at=T0 + timedelta(minutes=minutes),
    )
    db.add(batch)
    db.commit()
    db.add(
        GmailMessageIndex(
            owner_id=batch.owner_id,
            gmail_id=f"g{n}",
            thread_id=thread,
            message_id=batch.message_id,
            sent_at=batch.received_at,
        )
    )
    doc = None
    if docs:
        doc = Document(
            title=f"mail {n}",
            content="Inhalt",
            case_id=case_id,
            ingest_batch_id=batch.id,
            document_type=DocumentType.CORRESPONDENCE,
            thread_open=True,
        )
        db.add(doc)
    db.commit()
    return batch, doc


def _edges(db):
    db.expire_all()
    return db.query(DocumentRelationship).order_by(DocumentRelationship.id).all()


@pytest.mark.unit
def test_same_gmail_thread_links_with_references_and_keeps_thread_open(
    db_session, sample_case
):
    _, first = _mail(db_session, sample_case.id, 1, thread="T", minutes=0)
    second_b, second = _mail(db_session, sample_case.id, 2, thread="T", minutes=5)

    assert link_batch(db_session, second_b.id) == 1
    db_session.commit()

    (edge,) = _edges(db_session)
    assert (edge.from_document_id, edge.to_document_id) == (second.id, first.id)
    assert edge.relationship_type == RelationshipType.REFERENCES
    assert edge.confidence == RelationshipConfidence.EMAIL_HEADER
    assert edge.notes == "email header: Gmail thread"
    assert db_session.get(Document, first.id).thread_open is True


@pytest.mark.unit
def test_nearest_earlier_message_of_the_thread_wins(db_session, sample_case):
    _mail(db_session, sample_case.id, 1, thread="T", minutes=0)
    _, middle = _mail(db_session, sample_case.id, 2, thread="T", minutes=5)
    third_b, _ = _mail(db_session, sample_case.id, 3, thread="T", minutes=9)

    link_batch(db_session, third_b.id)
    db_session.commit()

    (edge,) = _edges(db_session)
    assert edge.to_document_id == middle.id


@pytest.mark.unit
def test_other_gmail_threads_are_not_linked(db_session, sample_case):
    _mail(db_session, sample_case.id, 1, thread="OTHER", minutes=0)
    only_b, _ = _mail(db_session, sample_case.id, 2, thread="T", minutes=5)

    assert link_batch(db_session, only_b.id) == 0
    assert _edges(db_session) == []


@pytest.mark.unit
def test_resolving_header_wins_over_the_thread_fallback(db_session, sample_case):
    _mail(db_session, sample_case.id, 1, thread="T", minutes=0)
    _, parent = _mail(db_session, sample_case.id, 2, thread="ELSEWHERE", minutes=1)
    reply_b, _ = _mail(
        db_session, sample_case.id, 3, thread="T", minutes=5, in_reply_to="<m2@x>"
    )

    link_batch(db_session, reply_b.id)
    db_session.commit()

    (edge,) = _edges(db_session)
    assert edge.to_document_id == parent.id
    assert edge.relationship_type == RelationshipType.REPLIES_TO


@pytest.mark.unit
def test_earlier_message_ingested_after_the_later_one_is_linked_on_arrival(
    db_session, sample_case
):
    first_b, _ = _mail(db_session, sample_case.id, 1, thread="T", minutes=0, docs=False)
    second_b, second = _mail(db_session, sample_case.id, 2, thread="T", minutes=5)
    assert link_batch(db_session, second_b.id) == 0  # first has no document yet

    first = Document(
        title="mail 1",
        content="Inhalt",
        case_id=sample_case.id,
        ingest_batch_id=first_b.id,
        document_type=DocumentType.CORRESPONDENCE,
    )
    db_session.add(first)
    db_session.commit()
    assert link_batch(db_session, first_b.id) == 1
    db_session.commit()

    (edge,) = _edges(db_session)
    assert (edge.from_document_id, edge.to_document_id) == (second.id, first.id)


@pytest.mark.unit
def test_a_nearer_thread_message_arriving_later_replaces_the_edge(
    db_session, sample_case
):
    _, oldest = _mail(db_session, sample_case.id, 1, thread="T", minutes=0)
    newest_b, newest = _mail(db_session, sample_case.id, 3, thread="T", minutes=9)
    link_batch(db_session, newest_b.id)
    db_session.commit()
    (first,) = _edges(db_session)
    assert first.to_document_id == oldest.id

    middle_b, middle = _mail(db_session, sample_case.id, 2, thread="T", minutes=5)
    link_batch(db_session, middle_b.id)
    db_session.commit()
    assert {e.notes for e in _edges(db_session)} == {"email header: Gmail thread"}

    by_edge = {(e.from_document_id, e.to_document_id) for e in _edges(db_session)}
    assert by_edge == {(newest.id, middle.id), (middle.id, oldest.id)}


@pytest.mark.unit
def test_without_index_rows_the_fallback_is_a_noop(db_session, sample_case):
    """A mailbox that was never indexed has no thread data: headers only."""
    _mail(db_session, sample_case.id, 1, thread="T", minutes=0)
    second_b, _ = _mail(db_session, sample_case.id, 2, thread="T", minutes=5)
    db_session.query(GmailMessageIndex).delete()
    db_session.commit()

    assert link_batch(db_session, second_b.id) == 0
    assert _edges(db_session) == []
