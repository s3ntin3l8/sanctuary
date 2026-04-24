import pytest

from app.models.database import Case, Claim, Document, IngestBatch
from app.models.enums import ClaimStatus, ClaimType, IngestBatchSourceType
from app.services.document_service import DocumentService


@pytest.mark.integration
def test_delete_document_with_claims(db_session):
    """Verify that deleting a document with associated claims works and doesn't trigger IntegrityError."""
    # 1. Setup
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
        case_id=case.id,
        source_document_id=doc.id,
        claim_text="This is a test claim",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
    )
    db_session.add(claim)
    db_session.commit()
    db_session.refresh(claim)

    claim_id = claim.id
    doc_id = doc.id

    # 2. Act
    doc_service = DocumentService(db_session)
    success = doc_service.delete_document(doc_id)

    # 3. Assert
    assert success is True

    # Verify document is gone
    deleted_doc = db_session.query(Document).filter(Document.id == doc_id).first()
    assert deleted_doc is None

    # Verify claim is also gone (as per our fix)
    deleted_claim = db_session.query(Claim).filter(Claim.id == claim_id).first()
    assert deleted_claim is None
