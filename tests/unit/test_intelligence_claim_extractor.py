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
    """A pre-existing claim owned by a *different* document. Cross-doc evidence
    links from `significant_doc` point at this claim — the realistic shape.

    (If the claim's source were `significant_doc` itself, retry-cleanup would
    delete it before the AI's evidence_link could fire, which doesn't reflect
    how the AI actually uses cross-doc evidence in production.)
    """
    other_doc = Document(
        title="Prior filing",
        content="Prior content asserting a fact.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
    )
    db_session.add(other_doc)
    db_session.flush()
    claim = Claim(
        case_id=sample_case.id,
        source_document_id=other_doc.id,
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

    # Source doc ASSERTS its own new claims (the canonical "originated by"
    # evidence row added in the Sharpen-Claims plan).
    evidence = db_session.query(ClaimEvidence).all()
    assert all(e.role == ClaimEvidenceRole.ASSERTS for e in evidence)
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


@pytest.mark.unit
def test_relay_doc_skipped(db_session, sample_case):
    """Cover letters (document_type=RELAY) yield only letterhead/metadata
    'claims' that the prompt explicitly prohibits. Skip the AI call entirely."""
    from app.models.enums import DocumentType

    doc = Document(
        title="Begleitschreiben Amtsgericht Hamburg",
        content="Anbei übersende ich Ihnen den Beschluss zur Kenntnisnahme.",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,  # tier passes the gate
        document_type=DocumentType.RELAY,  # but doc_type blocks
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

        result = extract(doc.id)
        mock_call.assert_not_called()
        assert result == "document_type:relay"


@pytest.mark.unit
def test_retry_clears_stale_asserted_claims(db_session, significant_doc, sample_case):
    """A repeat extraction on the same doc must not accumulate claims —
    delete prior auto-extracted ASSERTED claims before re-running."""
    # Seed three claims from a prior run, all in default ASSERTED state.
    for i in range(3):
        c = Claim(
            case_id=sample_case.id,
            source_document_id=significant_doc.id,
            claim_text=f"Stale claim from prior run number {i} that is long enough",
            claim_type=ClaimType.FACTUAL,
            status=ClaimStatus.ASSERTED,
            first_made_at=datetime.now(),
            last_updated_at=datetime.now(),
        )
        db_session.add(c)
    db_session.commit()
    assert (
        db_session.query(Claim)
        .filter(Claim.source_document_id == significant_doc.id)
        .count()
        == 3
    )

    ai_result = {
        "new_claims": [
            {
                "claim_text": "The new run produced this single fresh claim text",
                "claim_type": "factual",
                "excerpt": "fresh excerpt",
            }
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

    db_session.expire_all()
    remaining = (
        db_session.query(Claim)
        .filter(Claim.source_document_id == significant_doc.id)
        .all()
    )
    # Only the fresh claim from the latest run survives.
    assert len(remaining) == 1
    assert (
        remaining[0].claim_text == "The new run produced this single fresh claim text"
    )


@pytest.mark.unit
def test_retry_preserves_user_modified_claims(db_session, significant_doc, sample_case):
    """Claims with non-default status (CONTESTED/REFUTED/ESTABLISHED) carry
    cross-doc evidence signal or user edits and must survive a re-extraction."""
    # Three prior claims: one ASSERTED (stale), one CONTESTED (signal), one REFUTED.
    stale = Claim(
        case_id=sample_case.id,
        source_document_id=significant_doc.id,
        claim_text="Stale ASSERTED claim that should be deleted on retry",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    contested = Claim(
        case_id=sample_case.id,
        source_document_id=significant_doc.id,
        claim_text="A contested claim that another doc challenges and must persist",
        claim_type=ClaimType.LEGAL,
        status=ClaimStatus.CONTESTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    refuted = Claim(
        case_id=sample_case.id,
        source_document_id=significant_doc.id,
        claim_text="A refuted claim that has independent signal and must persist",
        claim_type=ClaimType.PROCEDURAL,
        status=ClaimStatus.REFUTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add_all([stale, contested, refuted])
    db_session.commit()

    ai_result = {"new_claims": [], "evidence_links": []}

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
    remaining = {
        c.status: c.claim_text
        for c in db_session.query(Claim)
        .filter(Claim.source_document_id == significant_doc.id)
        .all()
    }
    assert ClaimStatus.ASSERTED not in remaining, "stale ASSERTED claim must be deleted"
    assert ClaimStatus.CONTESTED in remaining, "CONTESTED claim must survive"
    assert ClaimStatus.REFUTED in remaining, "REFUTED claim must survive"


@pytest.mark.unit
def test_retry_preserves_evidence_pointing_at_other_docs_claims(
    db_session, significant_doc, sample_case
):
    """When this doc's claims are cleared on retry, claim_evidence rows that
    point at OTHER docs' claims (where this doc supports/contests them) must
    be preserved — they're independent of this doc's own derived data."""
    # Other doc owns a claim
    other_doc = Document(
        title="Other doc",
        content="content",
        case_id=sample_case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
    )
    db_session.add(other_doc)
    db_session.flush()

    other_claim = Claim(
        case_id=sample_case.id,
        source_document_id=other_doc.id,
        claim_text="Claim owned by another document",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(other_claim)
    db_session.flush()

    # significant_doc has its own ASSERTED claim (will be deleted on retry)
    own_claim = Claim(
        case_id=sample_case.id,
        source_document_id=significant_doc.id,
        claim_text="Own claim from prior extraction that will be cleared on retry",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(own_claim)
    db_session.flush()

    # significant_doc CONTESTS other_claim — this evidence must survive.
    cross_evidence = ClaimEvidence(
        claim_id=other_claim.id,
        document_id=significant_doc.id,
        role=ClaimEvidenceRole.CONTESTS,
        excerpt="cross-doc evidence",
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(cross_evidence)
    db_session.commit()

    ai_result = {"new_claims": [], "evidence_links": []}

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
    # other_claim must still exist (it was owned by other_doc, not cleared).
    assert db_session.get(Claim, other_claim.id) is not None
    # The cross-doc evidence pointing at other_claim must still exist.
    surviving = (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == other_claim.id)
        .all()
    )
    assert len(surviving) == 1
    assert surviving[0].role == ClaimEvidenceRole.CONTESTS
    # significant_doc's own ASSERTED claim was cleared.
    assert db_session.get(Claim, own_claim.id) is None
