"""Tests for Phase 4c claim extractor."""

from datetime import datetime
from unittest.mock import patch

import pytest

from app.models.database import Claim, ClaimEvidence, Document
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    OriginatorType,
    RelationshipConfidence,
    SignificanceTier,
)


@pytest.fixture
def significant_doc(db_session, sample_case):
    doc = Document(
        title="Klageerwiderung",
        content="Die Beklagte widerspricht der Klage. Sie bestreitet, an jenem Tag am Ort gewesen zu sein.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.OWN,
        ai_summary={
            "legal_significance": "Defense response",
            "required_action": "none",
            "financial_impact": "none",
        },
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.fixture
def existing_claim(db_session, sample_case, significant_doc):
    claim = Claim(
        case_id=sample_case.id,
        source_document_id=significant_doc.id,
        claim_text="Defendant was present at location on 2024-01-10",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.commit()
    db_session.refresh(claim)
    return claim


@pytest.mark.unit
def test_new_claims_created(db_session, significant_doc, sample_case):
    ai_result = {
        "new_claims": [
            {
                "claim_text": "The defendant was not present at the hearing on 15.03.2026",
                "claim_type": "factual",
                "excerpt": "She denies being there",
            },
            {
                "claim_text": "The contract was validly executed under § 433 BGB on 01.01.2024",
                "claim_type": "legal",
                "excerpt": "The contract was signed",
            },
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    claims = db_session.query(Claim).filter(Claim.case_id == sample_case.id).all()
    assert len(claims) == 2
    claim_texts = {c.claim_text for c in claims}
    assert "The defendant was not present at the hearing on 15.03.2026" in claim_texts
    assert (
        "The contract was validly executed under § 433 BGB on 01.01.2024" in claim_texts
    )

    # Source doc should be linked as SUPPORTS on its own new claims
    evidence = db_session.query(ClaimEvidence).all()
    assert all(e.role == ClaimEvidenceRole.SUPPORTS for e in evidence)
    assert all(e.document_id == significant_doc.id for e in evidence)
    assert all(e.confidence == RelationshipConfidence.AI_DETECTED for e in evidence)


@pytest.mark.unit
def test_evidence_link_contests_sets_status(
    db_session, significant_doc, existing_claim
):
    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {
                "claim_id": existing_claim.id,
                "role": "contests",
                "excerpt": "The defendant denies this",
            }
        ],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    db_session.expire_all()
    updated = db_session.get(Claim, existing_claim.id)
    assert updated.status == ClaimStatus.CONTESTED

    evidence = (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == existing_claim.id)
        .all()
    )
    assert len(evidence) == 1
    assert evidence[0].role == ClaimEvidenceRole.CONTESTS


@pytest.mark.unit
def test_evidence_link_refutes_sets_status(db_session, significant_doc, existing_claim):
    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {
                "claim_id": existing_claim.id,
                "role": "refutes",
                "excerpt": "Proven false by evidence",
            }
        ],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    db_session.expire_all()
    updated = db_session.get(Claim, existing_claim.id)
    assert updated.status == ClaimStatus.REFUTED


@pytest.mark.unit
def test_hallucination_guard_drops_invalid_claim_id(
    db_session, significant_doc, existing_claim
):
    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {"claim_id": 99999, "role": "supports", "excerpt": "Invented claim ID"},
            {"claim_id": existing_claim.id, "role": "supports", "excerpt": "Valid"},
        ],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    evidence = (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == existing_claim.id)
        .all()
    )
    # Only the valid link persisted
    assert len(evidence) == 1


@pytest.mark.unit
def test_hallucination_guard_drops_invalid_role(
    db_session, significant_doc, existing_claim
):
    ai_result = {
        "new_claims": [],
        "evidence_links": [
            {"claim_id": existing_claim.id, "role": "invalidrole", "excerpt": "test"},
        ],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    evidence = db_session.query(ClaimEvidence).all()
    assert len(evidence) == 0


@pytest.mark.unit
def test_hallucination_guard_drops_invalid_claim_type(db_session, significant_doc):
    ai_result = {
        "new_claims": [
            {
                "claim_text": "Something asserted",
                "claim_type": "unknowntype",
                "excerpt": "test",
            },
        ],
        "evidence_links": [],
    }

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync",
            return_value=ai_result,
        ),
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(significant_doc.id)

    claims = (
        db_session.query(Claim).filter(Claim.case_id == significant_doc.case_id).all()
    )
    assert len(claims) == 0


@pytest.mark.unit
def test_skips_administrative_tier(db_session, sample_case):
    doc = Document(
        title="Empfangsbestätigung",
        content="Wir bestätigen den Eingang.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.ADMINISTRATIVE,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    with (
        patch(
            "app.services.intelligence.claim_extractor.SessionLocal",
            return_value=db_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.claim_extractor._call_claim_extractor_sync"
        ) as mock_call,
    ):
        from app.services.intelligence.claim_extractor import extract

        extract(doc.id)
        mock_call.assert_not_called()
