"""Review flags survive confirmation, and contest/contradiction flags are clearable."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    Document,
    DocumentRelationship,
    User,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    OriginatorType,
    ProposalConfidence,
    ProposalStatus,
    RelationshipConfidence,
    RelationshipType,
)
from app.services.ingestion.service import (
    apply_review_reasons,
    compute_review_reasons,
    refresh_review_reasons,
)

pytestmark = pytest.mark.integration

client = TestClient(app)


def _doc(db, case_id, owner_id, title="Doc", **kw) -> Document:
    doc = Document(
        title=title,
        case_id=case_id,
        owner_id=owner_id,
        originator_type=OriginatorType.OWN,
        sender="me@example.com",
        received_date=datetime.now(UTC),
        issued_date=datetime.now(UTC),
        **kw,
    )
    db.add(doc)
    db.commit()
    return doc


def _owner(db) -> int:
    return db.query(User).filter_by(email="admin@localhost").one().id


def test_confirmed_document_stays_confirmed_after_refresh(db_session, sample_case):
    doc = _doc(db_session, sample_case.id, _owner(db_session))
    assert "pending_confirmation" in compute_review_reasons(doc)

    doc.confirmed_at = datetime.now(UTC)
    db_session.commit()
    refresh_review_reasons(doc, db_session)

    assert "pending_confirmation" not in doc.review_reasons
    assert doc.needs_review is False


def test_missing_parent_alone_does_not_hold_review(db_session, sample_case):
    from app.models.enums import DocumentRole

    doc = _doc(
        db_session,
        sample_case.id,
        _owner(db_session),
        role=DocumentRole.ENCLOSURE,
        confirmed_at=datetime.now(UTC),
    )
    apply_review_reasons(doc)
    assert doc.review_reasons == ["missing_parent"]
    assert doc.needs_review is False


def test_relationship_decision_does_not_reopen_confirmed_doc(db_session, sample_case):
    owner = _owner(db_session)
    src = _doc(db_session, sample_case.id, owner, "Src", confirmed_at=datetime.now(UTC))
    dst = _doc(db_session, sample_case.id, owner, "Dst", confirmed_at=datetime.now(UTC))
    rel = DocumentRelationship(
        from_document_id=src.id,
        to_document_id=dst.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(rel)
    db_session.commit()
    refresh_review_reasons(src, db_session)
    assert src.review_reasons == ["unresolved_relationship"]
    assert src.needs_review is True

    assert client.post(f"/api/v1/relationships/{rel.id}/confirm").status_code == 204

    db_session.refresh(src)
    assert src.review_reasons == []
    assert src.needs_review is False


def test_contradiction_acknowledge_clears_flag_and_survives_same_reenrich(
    db_session, sample_case
):
    doc = _doc(
        db_session,
        sample_case.id,
        _owner(db_session),
        confirmed_at=datetime.now(UTC),
        meta={"ai_contradiction": True, "contradiction_notes": ["claim X vs Y"]},
    )
    refresh_review_reasons(doc, db_session)
    assert doc.review_reasons == ["contradiction_detected"]

    resp = client.post(f"/api/v1/documents/{doc.id}/contradiction/acknowledge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_reasons"] == []
    assert body["contradiction_notes"] == []

    db_session.refresh(doc)
    assert doc.meta["contradiction_acknowledged"] == ["claim X vs Y"]


def _claim_asserted_in_case(db, case, owner_id) -> Claim:
    """The target claim must be linked to a case the viewer can edit."""
    asserter = _doc(db, case.id, owner_id, "Asserter", confirmed_at=datetime.now(UTC))
    claim = Claim(claim_text="Fact", status=ClaimStatus.ASSERTED)
    db.add(claim)
    db.flush()
    db.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=asserter.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db.commit()
    return claim


def _proposal(db, claim, doc, role=ClaimEvidenceRole.CONTESTS) -> ClaimEvidenceProposal:
    prop = ClaimEvidenceProposal(
        target_claim_id=claim.id,
        source_document_id=doc.id,
        proposed_role=role,
        excerpt="but actually",
        confidence=ProposalConfidence.HIGH,
    )
    db.add(prop)
    db.commit()
    return prop


@pytest.mark.parametrize("decision", ["confirm", "dismiss"])
def test_contest_flag_lifecycle(db_session, sample_case, decision):
    owner = _owner(db_session)
    doc = _doc(db_session, sample_case.id, owner, confirmed_at=datetime.now(UTC))
    claim = _claim_asserted_in_case(db_session, sample_case, owner)
    prop = _proposal(db_session, claim, doc)

    refresh_review_reasons(doc, db_session)
    assert "contests_existing_claim" in doc.review_reasons

    review = client.get(f"/api/v1/documents/{doc.id}/review").json()
    (shown,) = review["evidence_proposals"]
    assert shown["proposal_id"] == prop.id
    assert shown["target_claim_text"] == "Fact"

    resp = client.post(f"/api/v1/claims/proposals/evidence/{prop.id}/{decision}")
    assert resp.status_code == 204

    db_session.refresh(doc)
    assert "contests_existing_claim" not in doc.review_reasons
    assert doc.needs_review is False
    if decision == "confirm":
        ev = db_session.query(ClaimEvidence).filter_by(document_id=doc.id).one()
        assert ev.confidence == RelationshipConfidence.USER_CONFIRMED
        assert ev.role == ClaimEvidenceRole.CONTESTS
        db_session.refresh(prop)
        assert prop.status == ProposalStatus.CONFIRMED


def test_supports_proposal_does_not_flag_review(db_session, sample_case):
    doc = _doc(
        db_session, sample_case.id, _owner(db_session), confirmed_at=datetime.now(UTC)
    )
    claim = _claim_asserted_in_case(db_session, sample_case, _owner(db_session))
    _proposal(db_session, claim, doc, role=ClaimEvidenceRole.SUPPORTS)
    refresh_review_reasons(doc, db_session)
    assert doc.review_reasons == []


def test_migration_backfill(db_session, sample_case):
    import importlib.util
    from pathlib import Path

    from sqlalchemy import text

    from app.models.database import IngestBatch
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus

    path = next(Path(__file__).parents[2].glob("alembic/versions/b4e8c2a6d0f3_*.py"))
    spec = importlib.util.spec_from_file_location("confirmed_at_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    owner = _owner(db_session)
    done = IngestBatch(
        owner_id=owner,
        source_type=IngestBatchSourceType.EMAIL,
        subject="done",
        status=IngestBatchStatus.COMPLETED,
    )
    db_session.add(done)
    db_session.flush()
    in_done = _doc(
        db_session,
        sample_case.id,
        owner,
        "in-done-batch",
        ingest_batch_id=done.id,
        review_reasons=["pending_confirmation"],
    )
    cleared = _doc(
        db_session, sample_case.id, owner, "cleared", review_reasons=["missing_parent"]
    )
    waiting = _doc(
        db_session,
        sample_case.id,
        owner,
        "waiting",
        review_reasons=["pending_confirmation", "missing_sender"],
    )

    claim = _claim_asserted_in_case(db_session, sample_case, owner)
    prop = _proposal(db_session, claim, waiting)
    prop.status = ProposalStatus.CONFIRMED
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id,
            document_id=waiting.id,
            role=ClaimEvidenceRole.CONTESTS,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db_session.commit()
    for d in (in_done, cleared, waiting):
        d.confirmed_at = None
    db_session.commit()

    db_session.execute(text(mig.BACKFILL_CONFIRMED))
    db_session.execute(text(mig.BACKFILL_EVIDENCE_PROVENANCE))
    db_session.commit()
    for d in (in_done, cleared, waiting):
        db_session.refresh(d)

    assert in_done.confirmed_at is not None
    assert cleared.confirmed_at is not None
    assert waiting.confirmed_at is None
    ev = db_session.query(ClaimEvidence).filter_by(document_id=waiting.id).one()
    assert ev.confidence == RelationshipConfidence.USER_CONFIRMED
