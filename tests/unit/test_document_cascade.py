"""Unit tests for document cascade-delete — pins, reactions, and claim evidence."""

import pytest

from app.models.database import (
    Case,
    CaseStatus,
    Claim,
    ClaimEvidence,
    Document,
    DocumentPin,
    UserReaction,
)
from app.models.enums import ClaimEvidenceRole, UserReactionType
from app.services.document_service import DocumentService


@pytest.fixture
def case_and_doc(db_session):
    case = Case(id="CASCADE-001", title="Cascade Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()
    doc = Document(title="Cascade Target Doc", case_id=case.id)
    db_session.add(doc)
    db_session.commit()
    return case, doc


@pytest.mark.unit
def test_delete_document_removes_pins(db_session, case_and_doc):
    """Deleting a document removes its DocumentPin rows."""
    _, doc = case_and_doc
    pin = DocumentPin(
        document_id=doc.id, passage_id="abc123456789", user_id="single_user"
    )
    db_session.add(pin)
    db_session.commit()
    pin_id = pin.id

    svc = DocumentService(db_session)
    result = svc.delete_document(doc.id)

    assert result is True
    assert (
        db_session.query(DocumentPin).filter(DocumentPin.id == pin_id).first() is None
    )


@pytest.mark.unit
def test_delete_document_removes_reactions(db_session, case_and_doc):
    """Deleting a document removes its UserReaction rows."""
    _, doc = case_and_doc
    reaction = UserReaction(document_id=doc.id, reaction=UserReactionType.LIES)
    db_session.add(reaction)
    db_session.commit()
    reaction_id = reaction.id

    svc = DocumentService(db_session)
    svc.delete_document(doc.id)

    assert (
        db_session.query(UserReaction).filter(UserReaction.id == reaction_id).first()
        is None
    )


@pytest.mark.unit
def test_delete_document_removes_claim_evidence(db_session, case_and_doc):
    """Deleting a document removes ClaimEvidence rows linked to it."""
    case, doc = case_and_doc
    claim = Claim(
        case_id=case.id,
        source_document_id=doc.id,
        claim_text="Test claim",
    )
    db_session.add(claim)
    db_session.flush()
    evidence = ClaimEvidence(
        claim_id=claim.id,
        document_id=doc.id,
        role=ClaimEvidenceRole.SUPPORTS,
    )
    db_session.add(evidence)
    db_session.commit()
    evidence_id = evidence.id

    svc = DocumentService(db_session)
    svc.delete_document(doc.id)

    assert (
        db_session.query(ClaimEvidence).filter(ClaimEvidence.id == evidence_id).first()
        is None
    )


@pytest.mark.unit
def test_delete_document_removes_all_children(db_session, case_and_doc):
    """Deleting a document removes pins, reactions, and claim evidence together."""
    case, doc = case_and_doc

    pin = DocumentPin(
        document_id=doc.id, passage_id="pid111111111", user_id="single_user"
    )
    reaction = UserReaction(document_id=doc.id, reaction=UserReactionType.TRUE)
    db_session.add_all([pin, reaction])
    db_session.flush()

    claim = Claim(
        case_id=case.id, source_document_id=doc.id, claim_text="Multi child claim"
    )
    db_session.add(claim)
    db_session.flush()
    evidence = ClaimEvidence(
        claim_id=claim.id, document_id=doc.id, role=ClaimEvidenceRole.CONTESTS
    )
    db_session.add(evidence)
    db_session.commit()

    doc_id = doc.id
    svc = DocumentService(db_session)
    result = svc.delete_document(doc_id)

    assert result is True
    assert (
        db_session.query(DocumentPin).filter(DocumentPin.document_id == doc_id).count()
        == 0
    )
    assert (
        db_session.query(UserReaction)
        .filter(UserReaction.document_id == doc_id)
        .count()
        == 0
    )
    assert (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.document_id == doc_id)
        .count()
        == 0
    )
    assert db_session.query(Document).filter(Document.id == doc_id).first() is None
