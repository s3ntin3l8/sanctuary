"""TDD tests for ClaimService — Truth Map read side and status lifecycle."""

from datetime import datetime

import pytest

from app.models.database import Case, Claim, ClaimEvidence, Document, UserReaction
from app.models.enums import (
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    Jurisdiction,
    OriginatorType,
    RelationshipConfidence,
    UserReactionType,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cs_case(db_session):
    case = Case(
        id="CS-TRUTH-001",
        title="Truth Map Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.flush()
    return case


@pytest.fixture
def doc_a(db_session, cs_case):
    doc = Document(
        title="Klageerwiderung",
        content="Opposing statement",
        case_id=cs_case.id,
        originator_type=OriginatorType.OPPOSING,
        sender="opposing@example.com",
        received_date=datetime(2025, 3, 10),
    )
    db_session.add(doc)
    db_session.flush()
    db_session.refresh(doc)
    return doc


@pytest.fixture
def doc_b(db_session, cs_case):
    doc = Document(
        title="Jugendamtsbericht",
        content="Child services report",
        case_id=cs_case.id,
        originator_type=OriginatorType.COURT,
        sender="jugendamt@example.com",
        received_date=datetime(2025, 4, 5),
    )
    db_session.add(doc)
    db_session.flush()
    db_session.refresh(doc)
    return doc


def _make_claim(db_session, case, doc, text, status=ClaimStatus.ASSERTED):
    claim = Claim(
        case_id=case.id,
        source_document_id=doc.id,
        claim_text=text,
        claim_type=ClaimType.FACTUAL,
        status=status,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.flush()
    db_session.refresh(claim)
    return claim


def _make_evidence(db_session, claim, doc, role=ClaimEvidenceRole.SUPPORTS):
    ev = ClaimEvidence(
        claim_id=claim.id,
        document_id=doc.id,
        role=role,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(ev)
    db_session.flush()
    return ev


# ---------------------------------------------------------------------------
# get_truth_map — filter behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_truth_map_open_returns_asserted_and_contested(
    db_session, cs_case, doc_a, doc_b
):
    from app.services.claim_service import ClaimService

    contested = _make_claim(
        db_session, cs_case, doc_a, "Defendant was present", ClaimStatus.CONTESTED
    )
    asserted = _make_claim(
        db_session, cs_case, doc_a, "Contract was valid", ClaimStatus.ASSERTED
    )
    _make_claim(db_session, cs_case, doc_b, "Report is accurate", ClaimStatus.REFUTED)
    _make_claim(db_session, cs_case, doc_b, "Custody settled", ClaimStatus.ESTABLISHED)
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "open")

    statuses_in_view = {row.claim.id for group in view.groups for row in group.claims}
    assert contested.id in statuses_in_view
    assert asserted.id in statuses_in_view
    # refuted and established are excluded from "open"
    all_claim_ids = {row.claim.id for group in view.groups for row in group.claims}
    assert len(all_claim_ids) == 2


@pytest.mark.unit
def test_get_truth_map_open_groups_contested_before_asserted(
    db_session, cs_case, doc_a
):
    from app.services.claim_service import ClaimService

    _make_claim(db_session, cs_case, doc_a, "Claim A", ClaimStatus.ASSERTED)
    _make_claim(db_session, cs_case, doc_a, "Claim B", ClaimStatus.CONTESTED)
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "open")

    group_statuses = [g.status for g in view.groups if g.claims]
    assert group_statuses[0] == ClaimStatus.CONTESTED
    assert group_statuses[1] == ClaimStatus.ASSERTED


@pytest.mark.unit
def test_get_truth_map_all_returns_four_groups(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    for status in ClaimStatus:
        _make_claim(db_session, cs_case, doc_a, f"Claim {status}", status)
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "all")

    assert len(view.groups) == 4
    group_statuses = [g.status for g in view.groups]
    assert ClaimStatus.CONTESTED in group_statuses
    assert ClaimStatus.ASSERTED in group_statuses
    assert ClaimStatus.ESTABLISHED in group_statuses
    assert ClaimStatus.REFUTED in group_statuses


@pytest.mark.unit
def test_get_truth_map_open_count_always_reflects_open_claims(
    db_session, cs_case, doc_a
):
    """open_claim_count should be accurate even when viewing 'established' filter."""
    from app.services.claim_service import ClaimService

    _make_claim(db_session, cs_case, doc_a, "Claim A", ClaimStatus.CONTESTED)
    _make_claim(db_session, cs_case, doc_a, "Claim B", ClaimStatus.ASSERTED)
    _make_claim(db_session, cs_case, doc_a, "Claim C", ClaimStatus.ESTABLISHED)
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "established")

    assert view.open_claim_count == 2  # contested + asserted, not established


@pytest.mark.unit
def test_get_truth_map_excludes_other_case_claims(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    other_case = Case(
        id="CS-OTHER-001",
        title="Other Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    other_doc = Document(
        title="Other Doc",
        content="content",
        case_id="CS-OTHER-001",
        originator_type=OriginatorType.UNKNOWN,
        sender="x@x.com",
    )
    db_session.add(other_case)
    db_session.flush()
    db_session.add(other_doc)
    db_session.flush()

    _make_claim(db_session, cs_case, doc_a, "Our claim", ClaimStatus.ASSERTED)
    _make_claim(db_session, other_case, other_doc, "Their claim", ClaimStatus.ASSERTED)
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "all")

    all_ids = {row.claim.id for group in view.groups for row in group.claims}
    assert len(all_ids) == 1


# ---------------------------------------------------------------------------
# get_truth_map — evidence and reactions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_evidence_rows_attached_to_claim(db_session, cs_case, doc_a, doc_b):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "Claim with evidence", ClaimStatus.ASSERTED
    )
    _make_evidence(db_session, claim, doc_a, ClaimEvidenceRole.SUPPORTS)
    _make_evidence(db_session, claim, doc_b, ClaimEvidenceRole.CONTESTS)
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "open")

    claim_rows = [row for group in view.groups for row in group.claims]
    assert len(claim_rows) == 1
    assert len(claim_rows[0].evidence) == 2


@pytest.mark.unit
def test_evidence_ordered_by_received_date_asc(db_session, cs_case, doc_a, doc_b):
    from app.services.claim_service import ClaimService

    # doc_a received 2025-03-10, doc_b received 2025-04-05 → doc_a first
    claim = _make_claim(
        db_session, cs_case, doc_a, "Ordered evidence claim", ClaimStatus.ASSERTED
    )
    _make_evidence(db_session, claim, doc_b, ClaimEvidenceRole.CONTESTS)  # added second
    _make_evidence(db_session, claim, doc_a, ClaimEvidenceRole.SUPPORTS)  # added first
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "open")

    evidence_rows = view.groups[0].claims[0].evidence
    assert evidence_rows[0].document.id == doc_a.id
    assert evidence_rows[1].document.id == doc_b.id


@pytest.mark.unit
def test_reactions_attached_to_evidence_rows(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "Claim with reaction", ClaimStatus.ASSERTED
    )
    _make_evidence(db_session, claim, doc_a, ClaimEvidenceRole.SUPPORTS)

    reaction = UserReaction(
        document_id=doc_a.id,
        reaction=UserReactionType.LIES,
        notes="I dispute this",
    )
    db_session.add(reaction)
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "open")

    evidence_row = view.groups[0].claims[0].evidence[0]
    assert len(evidence_row.reactions) == 1
    assert evidence_row.reactions[0].reaction == UserReactionType.LIES


@pytest.mark.unit
def test_no_reactions_when_none_tagged(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "Claim no reaction", ClaimStatus.ASSERTED
    )
    _make_evidence(db_session, claim, doc_a, ClaimEvidenceRole.SUPPORTS)
    db_session.commit()

    svc = ClaimService(db_session)
    view = svc.get_truth_map(cs_case.id, "open")

    evidence_row = view.groups[0].claims[0].evidence[0]
    assert evidence_row.reactions == []


# ---------------------------------------------------------------------------
# transition_status — user lifecycle rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_transition_to_established_from_asserted(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "Will be established", ClaimStatus.ASSERTED
    )
    db_session.commit()

    svc = ClaimService(db_session)
    updated = svc.transition_status(claim.id, ClaimStatus.ESTABLISHED)

    assert updated.status == ClaimStatus.ESTABLISHED


@pytest.mark.unit
def test_transition_to_established_from_contested(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "Contested → established", ClaimStatus.CONTESTED
    )
    db_session.commit()

    svc = ClaimService(db_session)
    updated = svc.transition_status(claim.id, ClaimStatus.ESTABLISHED)

    assert updated.status == ClaimStatus.ESTABLISHED


@pytest.mark.unit
def test_reopen_asserted_from_established(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "Reopened claim", ClaimStatus.ESTABLISHED
    )
    db_session.commit()

    svc = ClaimService(db_session)
    updated = svc.transition_status(claim.id, ClaimStatus.ASSERTED)

    assert updated.status == ClaimStatus.ASSERTED


@pytest.mark.unit
def test_reopen_asserted_from_refuted(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "Refuted reopened", ClaimStatus.REFUTED
    )
    db_session.commit()

    svc = ClaimService(db_session)
    updated = svc.transition_status(claim.id, ClaimStatus.ASSERTED)

    assert updated.status == ClaimStatus.ASSERTED


@pytest.mark.unit
def test_transition_contested_raises_value_error(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "AI-owned state", ClaimStatus.ASSERTED
    )
    db_session.commit()

    svc = ClaimService(db_session)
    with pytest.raises(ValueError, match="AI-owned"):
        svc.transition_status(claim.id, ClaimStatus.CONTESTED)


@pytest.mark.unit
def test_transition_refuted_raises_value_error(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "AI-owned refuted", ClaimStatus.ASSERTED
    )
    db_session.commit()

    svc = ClaimService(db_session)
    with pytest.raises(ValueError, match="AI-owned"):
        svc.transition_status(claim.id, ClaimStatus.REFUTED)


@pytest.mark.unit
def test_transition_established_already_established_raises(db_session, cs_case, doc_a):
    from app.services.claim_service import ClaimService

    claim = _make_claim(
        db_session, cs_case, doc_a, "Already settled", ClaimStatus.ESTABLISHED
    )
    db_session.commit()

    svc = ClaimService(db_session)
    with pytest.raises(ValueError):
        svc.transition_status(claim.id, ClaimStatus.ESTABLISHED)


@pytest.mark.unit
def test_transition_unknown_claim_raises(db_session):
    from app.services.claim_service import ClaimService

    svc = ClaimService(db_session)
    with pytest.raises(ValueError, match="not found"):
        svc.transition_status(99999, ClaimStatus.ESTABLISHED)
