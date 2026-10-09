"""User edits clear their own review flags, survive AI re-runs, and leave no stale reasons."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    Document,
    DocumentRelationship,
    User,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    OriginatorType,
    ProposalConfidence,
    RelationshipConfidence,
    RelationshipType,
)
from app.services.document_service import DocumentService
from app.services.ingestion.service import (
    _apply_script_extractors,
    refresh_review_reasons,
)

pytestmark = pytest.mark.integration

client = TestClient(app)


def _owner(db) -> int:
    return db.query(User).filter_by(email="admin@localhost").one().id


def _doc(db, case_id, title="Doc", **kw) -> Document:
    fields = {
        "title": title,
        "case_id": case_id,
        "owner_id": _owner(db),
        "originator_type": OriginatorType.OWN,
        "sender": "me@example.com",
        "received_date": datetime.now(UTC),
        "issued_date": datetime.now(UTC),
        "confirmed_at": datetime.now(UTC),
        **kw,
    }
    doc = Document(**fields)
    db.add(doc)
    db.commit()
    return doc


def test_metadata_edit_clears_low_confidence_and_protects_the_edit(
    db_session, sample_case
):
    doc = _doc(
        db_session,
        sample_case.id,
        extraction_confidence={"sender": "low", "issued_date": "medium"},
    )
    refresh_review_reasons(doc, db_session)
    assert "low_confidence" in doc.review_reasons

    resp = client.put(
        f"/api/v1/documents/{doc.id}/metadata",
        json={"sender": "Gericht", "issued_date": "2026-01-02T00:00:00Z"},
    )
    assert resp.status_code == 200

    db_session.refresh(doc)
    assert doc.extraction_confidence["sender"] == "user_set"
    assert "low_confidence" not in doc.review_reasons
    assert doc.needs_review is False

    # A later EXTRACT run neither re-flags nor overwrites the edit.
    _apply_script_extractors(doc, "Absender: Someone Else\n", db_session)
    assert doc.sender == "Gericht"
    assert doc.extraction_confidence["sender"] == "user_set"


def test_extraction_alone_does_not_flag_low_confidence_from_az_court(
    db_session, sample_case
):
    doc = _doc(db_session, sample_case.id)
    _apply_script_extractors(doc, "Nothing recognisable here.", db_session)
    assert "az_court" not in doc.extraction_confidence


def test_deleting_a_document_clears_its_peers_relationship_reason(
    db_session, sample_case
):
    src = _doc(db_session, sample_case.id, "Src")
    dst = _doc(db_session, sample_case.id, "Dst")
    db_session.add(
        DocumentRelationship(
            from_document_id=src.id,
            to_document_id=dst.id,
            relationship_type=RelationshipType.REPLIES_TO,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db_session.commit()
    refresh_review_reasons(src, db_session)
    assert src.review_reasons == ["unresolved_relationship"]

    assert DocumentService(db_session).delete_document(dst.id)

    db_session.refresh(src)
    assert src.review_reasons == []
    assert src.needs_review is False


def test_merging_claims_keeps_pending_contests_on_the_survivor(db_session, sample_case):
    asserter = _doc(db_session, sample_case.id, "Asserter")
    contester = _doc(db_session, sample_case.id, "Contester")
    existing = Claim(claim_text="Fact", status=ClaimStatus.ASSERTED)
    absorbed = Claim(claim_text="Same fact", status=ClaimStatus.ASSERTED)
    db_session.add_all([existing, absorbed])
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=existing.id,
            document_id=asserter.id,
            role=ClaimEvidenceRole.ASSERTS,
        )
    )
    db_session.add(
        ClaimEvidence(
            claim_id=absorbed.id,
            document_id=contester.id,
            role=ClaimEvidenceRole.ASSERTS,
        )
    )
    db_session.add(
        ClaimEvidenceProposal(
            target_claim_id=absorbed.id,
            source_document_id=contester.id,
            proposed_role=ClaimEvidenceRole.CONTESTS,
            excerpt="but",
            confidence=ProposalConfidence.HIGH,
        )
    )
    merge = ClaimMergeProposal(
        new_claim_id=absorbed.id,
        existing_claim_id=existing.id,
        confidence=ProposalConfidence.HIGH,
    )
    db_session.add(merge)
    db_session.commit()
    refresh_review_reasons(contester, db_session)
    assert "contests_existing_claim" in contester.review_reasons

    resp = client.post(f"/api/v1/claims/proposals/merge/{merge.id}/confirm")
    assert resp.status_code == 204

    refresh_review_reasons(contester, db_session)
    assert "contests_existing_claim" in contester.review_reasons
    (prop,) = db_session.query(ClaimEvidenceProposal).all()
    assert prop.target_claim_id == existing.id


def test_confirming_a_loose_doc_into_a_draft_case_promotes_it(db_session, sample_case):
    sample_case.is_draft = True
    doc = _doc(db_session, "_TRIAGE", confirmed_at=None)
    db_session.commit()

    resp = client.post(
        "/api/v1/triage/confirm",
        json={"doc_id": doc.id, "case_id": sample_case.id, "action": "assign_case"},
    )
    assert resp.status_code == 200
    db_session.refresh(sample_case)
    assert sample_case.is_draft is False


def test_confirm_bundle_into_triage_is_rejected(db_session):
    doc = _doc(db_session, "_TRIAGE", confirmed_at=None)
    resp = client.post(
        "/api/v1/triage/confirm",
        json={"doc_id": doc.id, "case_id": "_TRIAGE", "action": "confirm_bundle"},
    )
    assert resp.status_code == 422
