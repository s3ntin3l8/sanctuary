from datetime import datetime

import pytest

from app.models.database import Claim, ClaimEvidence, Document, Entity
from app.models.enums import ClaimEvidenceRole, ClaimStatus, EntityType
from app.services.intelligence.claim_context import (
    format_claims_for_case,
    format_entities_for_case,
)


@pytest.mark.unit
def test_format_claims_for_case_empty_returns_empty_string(db_session, sample_case):
    assert format_claims_for_case(db_session, sample_case.id) == ""


@pytest.mark.unit
def test_format_claims_for_case_includes_status_and_evidence_counts(
    db_session, sample_case
):
    source_doc = Document(
        title="Source", content="x", case_id=sample_case.id, needs_review=False
    )
    db_session.add(source_doc)
    db_session.commit()

    claim = Claim(
        claim_text="Contested fact",
        status=ClaimStatus.CONTESTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.flush()

    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id,
                document_id=source_doc.id,
                role=ClaimEvidenceRole.ASSERTS,
                ingest_date=datetime.now(),
            ),
            ClaimEvidence(
                claim_id=claim.id,
                document_id=source_doc.id,
                role=ClaimEvidenceRole.SUPPORTS,
                ingest_date=datetime.now(),
            ),
            ClaimEvidence(
                claim_id=claim.id,
                document_id=source_doc.id,
                role=ClaimEvidenceRole.CONTESTS,
                ingest_date=datetime.now(),
            ),
        ]
    )
    db_session.commit()

    block = format_claims_for_case(db_session, sample_case.id)

    assert "Contested or Asserted Claims (Truth Map):" in block
    assert "[contested] Contested fact (Evidence: 1 supports, 1 contests)" in block


@pytest.mark.unit
def test_format_claims_for_case_excludes_dismissed(db_session, sample_case):
    source_doc = Document(
        title="Source", content="x", case_id=sample_case.id, needs_review=False
    )
    db_session.add(source_doc)
    db_session.commit()

    claim = Claim(
        claim_text="Dismissed claim",
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
        dismissed_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id,
            document_id=source_doc.id,
            role=ClaimEvidenceRole.ASSERTS,
            ingest_date=datetime.now(),
        )
    )
    db_session.commit()

    assert format_claims_for_case(db_session, sample_case.id) == ""


@pytest.mark.unit
def test_format_entities_for_case_empty_returns_empty_string(db_session, sample_case):
    assert format_entities_for_case(db_session, sample_case.id) == ""


@pytest.mark.unit
def test_format_entities_for_case_groups_by_type(db_session, sample_case):
    db_session.add_all(
        [
            Entity(case_id=sample_case.id, type=EntityType.PERSON, name="Zed Zorro"),
            Entity(case_id=sample_case.id, type=EntityType.PERSON, name="Anna Adler"),
            Entity(
                case_id=sample_case.id,
                type=EntityType.ORGANIZATION,
                name="Acme GmbH",
            ),
            # DATE entities exist but aren't in the default types tuple.
            Entity(case_id=sample_case.id, type=EntityType.DATE, name="2026-01-01"),
        ]
    )
    db_session.commit()

    block = format_entities_for_case(db_session, sample_case.id)

    assert "Key entities:" in block
    assert "People: Anna Adler, Zed Zorro" in block  # sorted by name
    assert "Organizations: Acme GmbH" in block
    assert "2026-01-01" not in block
