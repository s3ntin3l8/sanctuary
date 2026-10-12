from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.timezone import now_utc
from app.models.database import (
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    Document,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    ProposalConfidence,
    ProposalStatus,
    RelationshipConfidence,
    stronger_role,
)
from app.services.claim_proposal_service import confirm_evidence, confirm_merge


def _doc(db_session, case_id, title):
    doc = Document(title=title, content=title, case_id=case_id)
    db_session.add(doc)
    db_session.flush()
    return doc


def _claim(db_session, text="A fact"):
    claim = Claim(
        claim_text=text,
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.flush()
    return claim


def _evidence(
    db_session, claim, doc, role, confidence=RelationshipConfidence.AI_DETECTED
):
    ev = ClaimEvidence(
        claim_id=claim.id, document_id=doc.id, role=role, confidence=confidence
    )
    db_session.add(ev)
    db_session.flush()
    return ev


@pytest.mark.unit
def test_stronger_role_orders_adversarial_over_agreement():
    R = ClaimEvidenceRole
    assert stronger_role(R.CONTESTS, R.REFUTES) == R.REFUTES
    assert stronger_role(R.SUPPORTS, R.CONTESTS) == R.CONTESTS
    assert stronger_role(R.ASSERTS, R.REFUTES) == R.ASSERTS
    assert stronger_role(R.CITES_AS_PROOF, R.SUPPORTS) == R.SUPPORTS


@pytest.mark.unit
def test_confirm_evidence_updates_role_of_existing_row(db_session, sample_case):
    claim = _claim(db_session)
    doc = _doc(db_session, sample_case.id, "doc")
    _evidence(db_session, claim, doc, ClaimEvidenceRole.CITES_AS_PROOF)
    prop = ClaimEvidenceProposal(
        target_claim_id=claim.id,
        source_document_id=doc.id,
        proposed_role=ClaimEvidenceRole.REFUTES,
        excerpt="q",
        confidence=ProposalConfidence.MEDIUM,
        status=ProposalStatus.PENDING,
        proposed_at=now_utc(),
    )
    db_session.add(prop)
    db_session.flush()

    confirm_evidence(prop.id, db_session)
    db_session.flush()

    (row,) = db_session.query(ClaimEvidence).filter_by(claim_id=claim.id).all()
    assert row.role == ClaimEvidenceRole.REFUTES
    assert row.confidence == RelationshipConfidence.USER_CONFIRMED


@pytest.mark.unit
def test_confirm_evidence_keeps_the_asserting_role(db_session, sample_case):
    claim = _claim(db_session)
    doc = _doc(db_session, sample_case.id, "origin")
    _evidence(db_session, claim, doc, ClaimEvidenceRole.ASSERTS)
    prop = ClaimEvidenceProposal(
        target_claim_id=claim.id,
        source_document_id=doc.id,
        proposed_role=ClaimEvidenceRole.SUPPORTS,
        confidence=ProposalConfidence.MEDIUM,
        status=ProposalStatus.PENDING,
        proposed_at=now_utc(),
    )
    db_session.add(prop)
    db_session.flush()

    confirm_evidence(prop.id, db_session)
    db_session.flush()

    (row,) = db_session.query(ClaimEvidence).filter_by(claim_id=claim.id).all()
    assert row.role == ClaimEvidenceRole.ASSERTS


@pytest.mark.unit
def test_confirm_merge_keeps_stronger_role_on_collision(db_session, sample_case):
    survivor = _claim(db_session, "survivor")
    absorbed = _claim(db_session, "absorbed")
    shared = _doc(db_session, sample_case.id, "shared")
    only_absorbed = _doc(db_session, sample_case.id, "only absorbed")
    _evidence(db_session, survivor, shared, ClaimEvidenceRole.SUPPORTS)
    _evidence(db_session, absorbed, shared, ClaimEvidenceRole.REFUTES)
    _evidence(db_session, absorbed, only_absorbed, ClaimEvidenceRole.CONTESTS)
    prop = ClaimMergeProposal(
        new_claim_id=absorbed.id,
        existing_claim_id=survivor.id,
        confidence=ProposalConfidence.MEDIUM,
        status=ProposalStatus.PENDING,
        proposed_at=now_utc(),
    )
    db_session.add(prop)
    db_session.flush()
    absorbed_id = absorbed.id

    confirm_merge(prop.id, db_session)
    db_session.flush()
    db_session.expire_all()

    rows = {
        ev.document_id: ev.role
        for ev in db_session.query(ClaimEvidence).filter_by(claim_id=survivor.id)
    }
    assert rows == {
        shared.id: ClaimEvidenceRole.REFUTES,
        only_absorbed.id: ClaimEvidenceRole.CONTESTS,
    }
    assert db_session.get(Claim, absorbed_id) is None


@pytest.mark.unit
def test_db_rejects_a_second_link_for_the_same_pair(db_session, sample_case):
    claim = _claim(db_session)
    doc = _doc(db_session, sample_case.id, "doc")
    _evidence(db_session, claim, doc, ClaimEvidenceRole.CONTESTS)
    with pytest.raises(IntegrityError):
        _evidence(db_session, claim, doc, ClaimEvidenceRole.REFUTES)
    db_session.rollback()
