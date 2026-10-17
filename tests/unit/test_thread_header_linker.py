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


@pytest.mark.unit
def test_rejected_header_edge_is_not_recreated(db_session, sample_case):
    from app.repositories.document_relationship import reject_edge

    parent_b = _batch(db_session, "<p@x>")
    _doc(db_session, parent_b, sample_case.id)
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", refs=["<p@x>"])
    _doc(db_session, reply_b, sample_case.id)
    assert link_batch(db_session, reply_b.id) == 1
    db_session.commit()

    reject_edge(db_session, db_session.query(DocumentRelationship).one())
    db_session.commit()

    assert link_batch(db_session, reply_b.id) == 0
    assert link_batch(db_session, parent_b.id) == 0
    db_session.commit()
    assert _edges(db_session) == []


# --- the case follows the headers (#373) ------------------------------------


def _owned_case(db, sample_case, sample_user):
    sample_case.owner_id = sample_user.id
    db.commit()
    return sample_case


def _filed_parent(db, case, owner, **doc_kwargs):
    parent_b = _batch(db, "<p@x>", owner_id=owner.id)
    parent_b.case_id = case.id
    parent = _doc(db, parent_b, case.id, "original", **doc_kwargs)
    return parent_b, parent


@pytest.mark.unit
def test_a_reply_without_a_case_is_filed_where_its_parent_is(
    db_session, sample_case, sample_user
):
    from app.services import family_context

    case = _owned_case(db_session, sample_case, sample_user)
    _filed_parent(db_session, case, sample_user)
    reply_b = _batch(
        db_session,
        "<r@x>",
        in_reply_to="<p@x>",
        refs=["<p@x>"],
        owner_id=sample_user.id,
    )
    reply = _doc(db_session, reply_b, "_TRIAGE", "reply")

    link_batch(db_session, reply_b.id)
    db_session.commit()
    db_session.refresh(reply)
    db_session.refresh(reply_b)

    assert reply.case_id == case.id
    assert family_context.case_source(reply) == "inherited"
    assert reply_b.case_id == case.id


@pytest.mark.unit
def test_a_reply_with_a_case_of_its_own_keeps_it(db_session, sample_case, sample_user):
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction

    case = _owned_case(db_session, sample_case, sample_user)
    other = Case(
        id="OTHER-1",
        title="Other",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=sample_user.id,
    )
    db_session.add(other)
    db_session.commit()
    _filed_parent(db_session, case, sample_user)
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", owner_id=sample_user.id)
    reply = _doc(db_session, reply_b, other.id, "reply")

    link_batch(db_session, reply_b.id)
    db_session.commit()
    db_session.refresh(reply)

    assert reply.case_id == "OTHER-1"


@pytest.mark.unit
def test_a_reply_does_not_follow_a_parent_the_owner_cannot_edit(
    db_session, sample_case, sample_user
):
    # The case belongs to nobody the batch's owner could act as.
    sample_case.owner_id = None
    db_session.commit()
    _filed_parent(db_session, sample_case, sample_user)
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", owner_id=sample_user.id)
    reply = _doc(db_session, reply_b, "_TRIAGE", "reply")

    link_batch(db_session, reply_b.id)
    db_session.commit()
    db_session.refresh(reply)

    assert reply.case_id == "_TRIAGE"


@pytest.mark.unit
def test_the_gmail_thread_fallback_never_carries_a_case(
    db_session, sample_case, sample_user
):
    from app.services.intelligence import thread_header_linker as linker

    case = _owned_case(db_session, sample_case, sample_user)
    _, parent = _filed_parent(db_session, case, sample_user)
    reply_b = _batch(db_session, "<r@x>", owner_id=sample_user.id)
    reply = _doc(db_session, reply_b, "_TRIAGE", "reply")

    linker._inherit_case(
        db_session,
        reply_b,
        reply,
        linker._Parent(parent, RelationshipType.REFERENCES, linker._THREAD_NOTE),
    )

    assert reply.case_id == "_TRIAGE"


@pytest.mark.unit
def test_a_reply_that_arrived_first_is_filed_when_its_parent_is_linked(
    db_session, sample_case, sample_user
):
    case = _owned_case(db_session, sample_case, sample_user)
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", owner_id=sample_user.id)
    reply = _doc(db_session, reply_b, "_TRIAGE", "reply")
    link_batch(db_session, reply_b.id)  # the parent is not there yet
    db_session.commit()
    db_session.refresh(reply)
    assert reply.case_id == "_TRIAGE"

    parent_b, _ = _filed_parent(db_session, case, sample_user)
    link_batch(db_session, parent_b.id)
    db_session.commit()
    db_session.refresh(reply)

    assert reply.case_id == case.id


@pytest.mark.unit
def test_the_parents_proceeding_is_taken_only_without_an_az_of_its_own(
    db_session, sample_case, sample_user
):
    from app.models.database import Proceeding
    from app.models.enums import ProceedingCourtLevel, ProceedingStatus

    case = _owned_case(db_session, sample_case, sample_user)
    proc = Proceeding(
        case_id=case.id,
        court_name="Amtsgericht Ingolstadt",
        court_level=ProceedingCourtLevel.AG,
        az_court="3 C 1/26",
        status=ProceedingStatus.ACTIVE,
    )
    db_session.add(proc)
    db_session.commit()
    _filed_parent(db_session, case, sample_user, proceeding_id=proc.id)

    plain_b = _batch(db_session, "<r1@x>", in_reply_to="<p@x>", owner_id=sample_user.id)
    plain = _doc(db_session, plain_b, "_TRIAGE", "plain reply")
    other_b = _batch(db_session, "<r2@x>", in_reply_to="<p@x>", owner_id=sample_user.id)
    other = _doc(db_session, other_b, "_TRIAGE", "own AZ", az_court="9 C 9/26")

    link_batch(db_session, plain_b.id)
    link_batch(db_session, other_b.id)
    db_session.commit()
    db_session.refresh(plain)
    db_session.refresh(other)

    assert plain.proceeding_id == proc.id
    assert other.case_id == case.id
    assert other.proceeding_id != proc.id


@pytest.mark.unit
def test_a_rejected_header_edge_does_not_move_the_case(
    db_session, sample_case, sample_user
):
    from app.repositories.document_relationship import reject_edge

    case = _owned_case(db_session, sample_case, sample_user)
    _, parent = _filed_parent(db_session, case, sample_user)
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", owner_id=sample_user.id)
    reply = _doc(db_session, reply_b, "_TRIAGE", "reply")
    link_batch(db_session, reply_b.id)
    db_session.commit()
    db_session.refresh(reply)
    assert reply.case_id == case.id  # filed by the edge...

    (edge,) = _edges(db_session)
    reject_edge(db_session, edge)  # ...until the user says it is not part of it
    reply.case_id = "_TRIAGE"
    reply.extraction_confidence = {}
    db_session.commit()

    link_batch(db_session, reply_b.id)
    db_session.commit()
    db_session.refresh(reply)
    assert reply.case_id == "_TRIAGE"


@pytest.mark.unit
def test_an_inherited_case_follows_a_nearer_ancestor(
    db_session, sample_case, sample_user
):
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction

    first = _owned_case(db_session, sample_case, sample_user)
    second = Case(
        id="OTHER-2",
        title="Other",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=sample_user.id,
    )
    db_session.add(second)
    db_session.commit()
    grand_b = _batch(db_session, "<g@x>", owner_id=sample_user.id)
    grand_b.case_id = first.id
    _doc(db_session, grand_b, first.id, "earlier")
    reply_b = _batch(
        db_session,
        "<r@x>",
        in_reply_to="<mine@x>",
        refs=["<g@x>", "<mine@x>"],
        owner_id=sample_user.id,
    )
    reply = _doc(db_session, reply_b, "_TRIAGE", "reply")
    link_batch(db_session, reply_b.id)
    db_session.commit()
    db_session.refresh(reply)
    assert reply.case_id == first.id

    # A nearer ancestor, filed elsewhere, arrives later.
    near_b = _batch(db_session, "<mine@x>", owner_id=sample_user.id)
    near_b.case_id = second.id
    _doc(db_session, near_b, second.id, "nearer")
    link_batch(db_session, near_b.id)
    db_session.commit()
    db_session.refresh(reply)
    assert reply.case_id == second.id


@pytest.mark.unit
def test_a_case_the_reply_earned_is_never_replaced(
    db_session, sample_case, sample_user
):
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction
    from app.services import family_context

    case = _owned_case(db_session, sample_case, sample_user)
    _filed_parent(db_session, case, sample_user)
    other = Case(
        id="OTHER-3",
        title="Other",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=sample_user.id,
    )
    db_session.add(other)
    db_session.commit()
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", owner_id=sample_user.id)
    reply = _doc(db_session, reply_b, other.id, "reply")
    family_context.mark_case(reply, "user_set")
    db_session.commit()

    link_batch(db_session, reply_b.id)
    db_session.commit()
    db_session.refresh(reply)
    assert reply.case_id == other.id


@pytest.mark.unit
def test_filing_a_reply_clears_its_no_case_review_reason(
    db_session, sample_case, sample_user
):
    from app.services.ingestion.service import refresh_review_reasons

    case = _owned_case(db_session, sample_case, sample_user)
    reply_b = _batch(db_session, "<r@x>", in_reply_to="<p@x>", owner_id=sample_user.id)
    reply = _doc(db_session, reply_b, "_TRIAGE", "reply")
    refresh_review_reasons(reply, db_session, commit=True)
    assert "missing_case_id" in (reply.review_reasons or [])

    parent_b, _ = _filed_parent(db_session, case, sample_user)
    link_batch(db_session, parent_b.id)  # the parent arrives after its reply
    db_session.commit()
    db_session.refresh(reply)

    assert reply.case_id == case.id
    assert "missing_case_id" not in (reply.review_reasons or [])
