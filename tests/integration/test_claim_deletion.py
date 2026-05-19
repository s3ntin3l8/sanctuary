import pytest

from app.models.database import Case, Claim, ClaimEvidence, Document, IngestBatch
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    IngestBatchSourceType,
)
from app.services.document_service import DocumentService


@pytest.mark.integration
def test_delete_document_with_claims(db_session):
    """Wave 2A: deleting a document deletes claims that become rootless
    (no remaining evidence anywhere) and preserves claims that still have
    evidence on other documents."""
    case = Case(id="TEST-001", title="Test Case")
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="Claim Delete Test",
    )
    db_session.add(batch)
    db_session.commit()

    doc = Document(title="Doc with Claim", ingest_batch_id=batch.id, case_id=case.id)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    claim = Claim(
        claim_text="This is a test claim",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id,
            document_id=doc.id,
            role=ClaimEvidenceRole.ASSERTS,
        )
    )
    db_session.commit()
    db_session.refresh(claim)

    claim_id = claim.id
    doc_id = doc.id

    doc_service = DocumentService(db_session)
    success = doc_service.delete_document(doc_id)

    assert success is True

    deleted_doc = db_session.query(Document).filter(Document.id == doc_id).first()
    assert deleted_doc is None

    # Claim was rootless after the delete (only evidence was on the deleted
    # doc), so it gets cleaned up as part of the cascade.
    deleted_claim = db_session.query(Claim).filter(Claim.id == claim_id).first()
    assert deleted_claim is None


@pytest.mark.integration
def test_delete_document_preserves_cross_doc_claim(db_session):
    """A claim with evidence on multiple documents survives deletion of one
    of those documents — its remaining evidence keeps it scoped to a case."""
    case = Case(id="TEST-002", title="Case 2")
    db_session.add(case)
    db_session.flush()
    batch = IngestBatch(source_type=IngestBatchSourceType.EMAIL, subject="b")
    db_session.add(batch)
    db_session.flush()
    doc_a = Document(title="A", ingest_batch_id=batch.id, case_id=case.id)
    doc_b = Document(title="B", ingest_batch_id=batch.id, case_id=case.id)
    db_session.add_all([doc_a, doc_b])
    db_session.flush()

    claim = Claim(
        claim_text="Cross-doc claim",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id,
                document_id=doc_a.id,
                role=ClaimEvidenceRole.ASSERTS,
            ),
            ClaimEvidence(
                claim_id=claim.id,
                document_id=doc_b.id,
                role=ClaimEvidenceRole.SUPPORTS,
            ),
        ]
    )
    db_session.commit()

    DocumentService(db_session).delete_document(doc_a.id)

    survived = db_session.query(Claim).filter(Claim.id == claim.id).first()
    assert survived is not None, (
        "claim still has evidence on doc_b; it must not be deleted"
    )
    remaining = (
        db_session.query(ClaimEvidence).filter(ClaimEvidence.claim_id == claim.id).all()
    )
    assert len(remaining) == 1
    assert remaining[0].document_id == doc_b.id


@pytest.mark.integration
def test_find_duplicates_guard_blocks_concurrent(db_session):
    """A second find-duplicates click while a job is running must not reset
    progress — it should render the running fragment against the existing job.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import user_settings_service as uss

    case = Case(id="DEDUP-GUARD-001", title="Dedup Guard Test")
    db_session.add(case)
    db_session.commit()

    # Seed a running job with non-trivial progress.
    uss.set_dedup_running(case.id, db_session, total=50)
    db_session.commit()
    # Set processed > 0 so we can detect a reset.
    uss.update_dedup_progress(case.id, db_session, processed=12)
    db_session.commit()

    client = TestClient(app)
    response = client.post(f"/cases/{case.id}/claims/find-duplicates")

    assert response.status_code == 200
    # Guard should NOT have reset processed back to 0.
    db_session.expire_all()
    job = uss.get_dedup_job(case.id, db_session)
    assert job is not None
    assert job["status"] == "running"
    assert job["processed"] == 12, "concurrent click reset processed; guard didn't fire"
