"""Direct unit coverage for access_guards.py's claim access model.

No live route exercises claim_access_allowed(edit=False) (every claims.py
route that mutates uses edit=True; view-level claim access is only reachable
indirectly, e.g. via the truthmap's case-level guard). These tests call the
guard functions directly to pin down the "view = any linked case, edit = all
linked cases" contract, plus the _TRIAGE-only ownership fallback — flagged as
untested in PR #160's review.
"""

import pytest

from app.api.access_guards import claim_access_allowed
from app.models.database import Case, Claim, ClaimEvidence, Document
from app.models.enums import CaseStatus, ClaimEvidenceRole, ClaimStatus, Jurisdiction
from app.services import auth_service


def _make_case(db, case_id, owner_id):
    case = Case(
        id=case_id,
        title=f"Case {case_id}",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=owner_id,
    )
    db.add(case)
    db.commit()
    return case


@pytest.fixture
def two_users(db_session):
    a = auth_service.create_user(
        db_session, email="a@example.com", password="password123"
    )
    b = auth_service.create_user(
        db_session, email="b@example.com", password="password123"
    )
    db_session.commit()
    return a, b


def _claim_with_evidence(db, *docs_and_roles):
    claim = Claim(claim_text="Fact", status=ClaimStatus.ASSERTED)
    db.add(claim)
    db.flush()
    for doc, role in docs_and_roles:
        db.add(ClaimEvidence(claim_id=claim.id, document_id=doc.id, role=role))
    db.commit()
    return claim


@pytest.mark.unit
def test_view_allowed_via_either_linked_case(db_session, two_users):
    """A claim spanning two cases: viewing requires access to just one of them."""
    a, b = two_users
    case_a = _make_case(db_session, "GUARD-A", a.id)
    case_b = _make_case(db_session, "GUARD-B", b.id)
    doc_a = Document(title="A doc", owner_id=a.id, case_id=case_a.id)
    doc_b = Document(title="B doc", owner_id=b.id, case_id=case_b.id)
    db_session.add_all([doc_a, doc_b])
    db_session.flush()
    claim = _claim_with_evidence(
        db_session,
        (doc_a, ClaimEvidenceRole.ASSERTS),
        (doc_b, ClaimEvidenceRole.SUPPORTS),
    )

    assert claim_access_allowed(db_session, a, claim.id, edit=False) is True
    assert claim_access_allowed(db_session, b, claim.id, edit=False) is True


@pytest.mark.unit
def test_edit_requires_both_linked_cases(db_session, two_users):
    """Editing the same cross-case claim requires access to *every* linked
    case — neither A nor B alone can edit it, only an admin (or a user who
    owns/is shared on both) could."""
    a, b = two_users
    case_a = _make_case(db_session, "GUARD-A", a.id)
    case_b = _make_case(db_session, "GUARD-B", b.id)
    doc_a = Document(title="A doc", owner_id=a.id, case_id=case_a.id)
    doc_b = Document(title="B doc", owner_id=b.id, case_id=case_b.id)
    db_session.add_all([doc_a, doc_b])
    db_session.flush()
    claim = _claim_with_evidence(
        db_session,
        (doc_a, ClaimEvidenceRole.ASSERTS),
        (doc_b, ClaimEvidenceRole.SUPPORTS),
    )

    assert claim_access_allowed(db_session, a, claim.id, edit=True) is False
    assert claim_access_allowed(db_session, b, claim.id, edit=True) is False


@pytest.mark.unit
def test_triage_only_claim_ownership_fallback_view(db_session, two_users):
    """A claim with no real-case evidence yet (only _TRIAGE documents) falls
    back to ownership: either owner can view it."""
    a, b = two_users
    doc_a = Document(title="A triage doc", owner_id=a.id, case_id="_TRIAGE")
    doc_b = Document(title="B triage doc", owner_id=b.id, case_id="_TRIAGE")
    db_session.add_all([doc_a, doc_b])
    db_session.flush()
    claim = _claim_with_evidence(
        db_session,
        (doc_a, ClaimEvidenceRole.ASSERTS),
        (doc_b, ClaimEvidenceRole.SUPPORTS),
    )

    assert claim_access_allowed(db_session, a, claim.id, edit=False) is True
    assert claim_access_allowed(db_session, b, claim.id, edit=False) is True


@pytest.mark.unit
def test_triage_only_claim_ownership_fallback_edit_requires_sole_owner(
    db_session, two_users
):
    """Editing a _TRIAGE-only claim evidenced by two different owners'
    documents must not be unilaterally allowed for either owner — same
    all-vs-any asymmetry as the real-case branch."""
    a, b = two_users
    doc_a = Document(title="A triage doc", owner_id=a.id, case_id="_TRIAGE")
    doc_b = Document(title="B triage doc", owner_id=b.id, case_id="_TRIAGE")
    db_session.add_all([doc_a, doc_b])
    db_session.flush()
    claim = _claim_with_evidence(
        db_session,
        (doc_a, ClaimEvidenceRole.ASSERTS),
        (doc_b, ClaimEvidenceRole.SUPPORTS),
    )

    assert claim_access_allowed(db_session, a, claim.id, edit=True) is False
    assert claim_access_allowed(db_session, b, claim.id, edit=True) is False

    # A claim evidenced only by A's own triage docs: A alone may edit it.
    solo_claim = _claim_with_evidence(db_session, (doc_a, ClaimEvidenceRole.ASSERTS))
    assert claim_access_allowed(db_session, a, solo_claim.id, edit=True) is True
    assert claim_access_allowed(db_session, b, solo_claim.id, edit=True) is False
