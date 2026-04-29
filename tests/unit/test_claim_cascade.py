"""Pin: deleting a Claim cascades to its ClaimEvidence rows.

The truth map is built on these two tables together. Stranded ClaimEvidence
rows would corrupt AI context retrieval (they reference dead claim_ids and
the case-brief generator can't recover from that).

ORM cascade is configured via `cascade="all, delete-orphan"` on
`Claim.evidence` (see `database.py:404`). This test pins that wiring.
"""

import pytest

from app.models.database import (
    Case,
    Claim,
    ClaimEvidence,
    Document,
    IngestBatch,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimType,
    IngestBatchSourceType,
)


@pytest.fixture
def claim_with_evidence(db_session):
    case = Case(id="CL-CASCADE-1", title="Cascade test")
    db_session.add(case)
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
    db_session.add(batch)
    db_session.commit()

    doc = Document(
        title="Source", content="x", ingest_batch_id=batch.id, case_id=case.id
    )
    db_session.add(doc)
    db_session.commit()

    claim = Claim(
        case_id=case.id,
        source_document_id=doc.id,
        claim_text="X claims Y",
        claim_type=ClaimType.FACTUAL,
    )
    db_session.add(claim)
    db_session.commit()

    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id,
                document_id=doc.id,
                role=ClaimEvidenceRole.SUPPORTS,
            ),
            ClaimEvidence(
                claim_id=claim.id,
                document_id=doc.id,
                role=ClaimEvidenceRole.CONTESTS,
            ),
        ]
    )
    db_session.commit()
    return claim, doc


@pytest.mark.unit
def test_delete_claim_cascades_to_evidence(db_session, claim_with_evidence):
    claim, _doc = claim_with_evidence
    evidence_count_before = (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == claim.id)
        .count()
    )
    assert evidence_count_before == 2

    db_session.delete(claim)
    db_session.commit()

    db_session.expire_all()
    assert db_session.get(Claim, claim.id) is None
    orphan_count = (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == claim.id)
        .count()
    )
    assert orphan_count == 0, (
        f"Expected 0 orphan ClaimEvidence rows after Claim delete, got {orphan_count}"
    )


@pytest.mark.unit
def test_delete_document_via_service_removes_dependent_claim_chain(
    db_session, claim_with_evidence
):
    """When the source document of a Claim is deleted via DocumentService,
    the Claim and its ClaimEvidence rows must be removed too — not stranded
    with dangling source_document_id."""
    from app.services.document_service import DocumentService

    claim, doc = claim_with_evidence
    doc_id = doc.id
    claim_id = claim.id
    DocumentService(db_session).delete_document(doc_id)

    db_session.expire_all()
    assert db_session.query(Document).filter(Document.id == doc_id).count() == 0
    assert db_session.query(Claim).filter(Claim.id == claim_id).count() == 0
    assert (
        db_session.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == claim_id)
        .count()
        == 0
    )
