import pytest

from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType


@pytest.mark.integration
def test_delete_document_triage_context_oob(app_client, db_session):
    # 1. Setup a batch with 2 documents
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="Triage Delete Test",
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)

    doc1 = Document(title="Doc 1", ingest_batch_id=batch.id, case_id="_TRIAGE")
    doc2 = Document(title="Doc 2", ingest_batch_id=batch.id, case_id="_TRIAGE")
    db_session.add_all([doc1, doc2])
    db_session.commit()

    batch_id = batch.id

    # 2. Delete doc1 with context=triage
    response = app_client.delete(f"/document/{doc1.id}?context=triage")

    assert response.status_code == 200
    # Should return an OOB swap for the bundle group because doc2 still exists
    assert f'id="triage-bundle-group-batch-{batch_id}"' in response.text
    assert 'hx-swap-oob="true"' in response.text

    # 3. Delete doc2 with context=triage
    response = app_client.delete(f"/document/{doc2.id}?context=triage")

    assert response.status_code == 200
    # Now it should show the "Queue Clear" state because it was the last doc in the last bundle
    assert 'id="triage-feed"' in response.text
    assert 'hx-swap-oob="true"' in response.text
    assert "Triage Queue Is Clear" in response.text


@pytest.mark.integration
def test_delete_document_last_in_bundle_not_last_in_queue(app_client, db_session):
    # 1. Setup 2 batches with 1 document each
    batch1 = IngestBatch(source_type=IngestBatchSourceType.EMAIL, subject="Batch 1")
    batch2 = IngestBatch(source_type=IngestBatchSourceType.EMAIL, subject="Batch 2")
    db_session.add_all([batch1, batch2])
    db_session.commit()

    doc1 = Document(title="Doc 1", ingest_batch_id=batch1.id, case_id="_TRIAGE")
    doc2 = Document(title="Doc 2", ingest_batch_id=batch2.id, case_id="_TRIAGE")
    db_session.add_all([doc1, doc2])
    db_session.commit()

    batch1_id = batch1.id

    # 2. Delete doc1 with context=triage
    response = app_client.delete(f"/document/{doc1.id}?context=triage")

    assert response.status_code == 200
    # Should return a "delete" OOB swap for the group because batch1 is now empty, but batch2 remains
    assert f'id="triage-bundle-group-batch-{batch1_id}"' in response.text
    assert 'hx-swap-oob="delete"' in response.text
    # Should NOT return the full feed because batch2 still exists
    assert "Triage Queue Is Clear" not in response.text
