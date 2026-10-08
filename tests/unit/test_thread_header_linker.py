"""Deterministic reply edges from In-Reply-To / References (issue #58)."""

import pytest

from app.models.database import Document, DocumentRelationship, IngestBatch
from app.models.enums import (
    DocumentRole,
    DocumentType,
    IngestBatchSourceType,
    RelationshipConfidence,
    RelationshipType,
)
from app.services.intelligence.thread_header_linker import (
    lead_document,
    link_batch,
)


def _batch(db, message_id, *, in_reply_to=None, refs=None, owner_id=None):
    kwargs = {"owner_id": owner_id} if owner_id is not None else {}
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        message_id=message_id,
        in_reply_to=in_reply_to,
        thread_refs=refs,
        **kwargs,
    )
    db.add(batch)
    db.commit()
    return batch


def _doc(db, batch, case_id, title="letter", **kwargs):
    doc = Document(
        title=title,
        content="Inhalt des Schreibens",
        case_id=case_id,
        ingest_batch_id=batch.id,
        document_type=kwargs.pop("document_type", DocumentType.CORRESPONDENCE),
        thread_open=kwargs.pop("thread_open", True),
        **kwargs,
    )
    db.add(doc)
    db.commit()
    return doc


def _edges(db):
    db.expire_all()
    return db.query(DocumentRelationship).order_by(DocumentRelationship.id).all()


@pytest.mark.unit
def test_in_reply_to_creates_replies_to_edge_and_closes_thread(db_session, sample_case):
    parent_b = _batch(db_session, "<p@x>")
    parent = _doc(db_session, parent_b, sample_case.id, "original")
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", refs=["<p@x>"])
    reply = _doc(db_session, reply_b, sample_case.id, "reply")

    assert link_batch(db_session, reply_b.id) == 1
    db_session.commit()

    (edge,) = _edges(db_session)
    assert edge.from_document_id == reply.id
    assert edge.to_document_id == parent.id
    assert edge.relationship_type == RelationshipType.REPLIES_TO
    assert edge.confidence == RelationshipConfidence.EMAIL_HEADER
    assert db_session.get(Document, parent.id).thread_open is False


@pytest.mark.unit
def test_references_only_match_is_references_edge_and_keeps_thread_open(
    db_session, sample_case
):
    """The real parent (the user's own mail) was never ingested."""
    grand_b = _batch(db_session, "<g@x>")
    grand = _doc(db_session, grand_b, sample_case.id, "earlier letter")
    reply_b = _batch(
        db_session, "<r@x>", in_reply_to="<mine@x>", refs=["<g@x>", "<mine@x>"]
    )
    reply = _doc(db_session, reply_b, sample_case.id, "reply")

    assert link_batch(db_session, reply_b.id) == 1
    db_session.commit()

    (edge,) = _edges(db_session)
    assert (edge.from_document_id, edge.to_document_id) == (reply.id, grand.id)
    assert edge.relationship_type == RelationshipType.REFERENCES
    assert edge.confidence == RelationshipConfidence.EMAIL_HEADER
    assert db_session.get(Document, grand.id).thread_open is True


@pytest.mark.unit
def test_nearest_ingested_ancestor_wins(db_session, sample_case):
    a_b = _batch(db_session, "<a@x>")
    b_b = _batch(db_session, "<b@x>")
    _doc(db_session, a_b, sample_case.id, "a")
    b_doc = _doc(db_session, b_b, sample_case.id, "b")
    reply_b = _batch(
        db_session,
        "<r@x>",
        in_reply_to="<c@x>",
        refs=["<a@x>", "<b@x>", "<c@x>"],
    )
    _doc(db_session, reply_b, sample_case.id, "reply")

    link_batch(db_session, reply_b.id)
    db_session.commit()

    (edge,) = _edges(db_session)
    assert edge.to_document_id == b_doc.id
    assert edge.relationship_type == RelationshipType.REFERENCES


@pytest.mark.unit
def test_reply_ingested_before_parent_is_linked_when_parent_arrives(
    db_session, sample_case
):
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", refs=["<p@x>"])
    reply = _doc(db_session, reply_b, sample_case.id, "reply")
    assert link_batch(db_session, reply_b.id) == 0
    assert _edges(db_session) == []

    parent_b = _batch(db_session, "<p@x>")
    parent = _doc(db_session, parent_b, sample_case.id, "original")
    assert link_batch(db_session, parent_b.id) == 1
    db_session.commit()

    (edge,) = _edges(db_session)
    assert (edge.from_document_id, edge.to_document_id) == (reply.id, parent.id)
    assert edge.relationship_type == RelationshipType.REPLIES_TO
    assert db_session.get(Document, parent.id).thread_open is False


@pytest.mark.unit
def test_nearer_parent_arriving_later_replaces_stale_references_edge(
    db_session, sample_case
):
    grand_b = _batch(db_session, "<g@x>")
    grand = _doc(db_session, grand_b, sample_case.id, "grandparent")
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", refs=["<g@x>", "<p@x>"])
    reply = _doc(db_session, reply_b, sample_case.id, "reply")
    link_batch(db_session, reply_b.id)
    db_session.commit()
    (first,) = _edges(db_session)
    assert first.to_document_id == grand.id
    assert first.relationship_type == RelationshipType.REFERENCES

    parent_b = _batch(db_session, "<p@x>")
    parent = _doc(db_session, parent_b, sample_case.id, "parent")
    link_batch(db_session, parent_b.id)
    db_session.commit()

    (edge,) = _edges(db_session)
    assert (edge.from_document_id, edge.to_document_id) == (reply.id, parent.id)
    assert edge.relationship_type == RelationshipType.REPLIES_TO


@pytest.mark.unit
def test_cover_letter_is_the_lead_and_enclosures_are_ignored(db_session, sample_case):
    parent_b = _batch(db_session, "<p@x>")
    cover = _doc(
        db_session, parent_b, sample_case.id, "cover", role=DocumentRole.COVER_LETTER
    )
    _doc(
        db_session,
        parent_b,
        sample_case.id,
        "enclosure",
        role=DocumentRole.ENCLOSURE,
        parent_id=cover.id,
    )

    assert lead_document(db_session, parent_b.id).id == cover.id


@pytest.mark.unit
def test_two_cover_letters_or_many_roots_are_ambiguous(db_session, sample_case):
    two_covers = _batch(db_session, "<c@x>")
    _doc(db_session, two_covers, sample_case.id, role=DocumentRole.COVER_LETTER)
    _doc(db_session, two_covers, sample_case.id, role=DocumentRole.COVER_LETTER)
    assert lead_document(db_session, two_covers.id) is None

    many_roots = _batch(db_session, "<m@x>")
    _doc(db_session, many_roots, sample_case.id)
    _doc(db_session, many_roots, sample_case.id)
    assert lead_document(db_session, many_roots.id) is None


@pytest.mark.unit
def test_body_document_leads_an_attachmentless_email(db_session, sample_case):
    batch = _batch(db_session, "<b@x>")
    body = _doc(
        db_session,
        batch,
        sample_case.id,
        original_filename=f"email_body_{batch.id}.txt",
    )
    assert lead_document(db_session, batch.id).id == body.id


@pytest.mark.unit
def test_ambiguous_parent_batch_is_skipped_for_the_next_ancestor(
    db_session, sample_case
):
    grand_b = _batch(db_session, "<g@x>")
    grand = _doc(db_session, grand_b, sample_case.id, "grand")
    parent_b = _batch(db_session, "<p@x>")
    _doc(db_session, parent_b, sample_case.id)
    _doc(db_session, parent_b, sample_case.id)  # two roots → no lead
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", refs=["<g@x>", "<p@x>"])
    _doc(db_session, reply_b, sample_case.id)

    link_batch(db_session, reply_b.id)
    db_session.commit()

    (edge,) = _edges(db_session)
    assert edge.to_document_id == grand.id
    assert edge.relationship_type == RelationshipType.REFERENCES


@pytest.mark.unit
def test_other_owners_batches_are_never_linked(db_session, sample_case, sample_user):
    parent_b = _batch(db_session, "<p@x>", owner_id=sample_user.id)
    _doc(db_session, parent_b, sample_case.id)
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", refs=["<p@x>"])
    _doc(db_session, reply_b, sample_case.id)
    assert reply_b.owner_id != sample_user.id

    assert link_batch(db_session, reply_b.id) == 0
    assert link_batch(db_session, parent_b.id) == 0
    assert _edges(db_session) == []


@pytest.mark.unit
def test_ai_edge_is_upgraded_but_user_edge_is_kept(db_session, sample_case):
    parent_b = _batch(db_session, "<p@x>")
    parent = _doc(db_session, parent_b, sample_case.id)
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", refs=["<p@x>"])
    reply = _doc(
        db_session,
        reply_b,
        sample_case.id,
        review_reasons=["unresolved_relationship"],
        needs_review=True,
    )
    ai = DocumentRelationship(
        from_document_id=reply.id,
        to_document_id=parent.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(ai)
    db_session.commit()

    link_batch(db_session, reply_b.id)
    db_session.commit()
    (edge,) = _edges(db_session)
    assert edge.confidence == RelationshipConfidence.EMAIL_HEADER
    # The upgraded edge no longer counts as an unresolved AI suggestion.
    assert "unresolved_relationship" not in (
        db_session.get(Document, reply.id).review_reasons or []
    )

    edge.confidence = RelationshipConfidence.USER_CONFIRMED
    db_session.commit()
    link_batch(db_session, reply_b.id)
    db_session.commit()
    (edge,) = _edges(db_session)
    assert edge.confidence == RelationshipConfidence.USER_CONFIRMED


@pytest.mark.unit
def test_batch_without_headers_is_a_noop(db_session, sample_case):
    batch = _batch(db_session, "<solo@x>")
    _doc(db_session, batch, sample_case.id)
    assert link_batch(db_session, batch.id) == 0
    assert _edges(db_session) == []
