from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, PipelineState


def test_dismiss_batch_success(app_client, db_session):
    # Create another batch to keep triage non-empty
    other_batch = IngestBatch(
        subject="Other Batch", source_type=IngestBatchSourceType.EMAIL
    )
    db_session.add(other_batch)
    db_session.commit()
    other_doc = Document(title="Other Doc", ingest_batch_id=other_batch.id)
    db_session.add(other_doc)
    db_session.commit()
    # Create a dummy ingest batch
    batch = IngestBatch(subject="Test Batch", source_type=IngestBatchSourceType.EMAIL)
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)

    # Create a document in this batch that needs triage
    doc = Document(
        title="Test Doc",
        ingest_batch_id=batch.id,
        pipeline_state=PipelineState.PENDING,
        significance_tier="significant",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    response = app_client.post(f"/triage/dismiss?batch_id={batch.id}")

    assert response.status_code == 200
    assert f'id="triage-row-batch-{batch.id}"' in response.text
    assert 'hx-swap-oob="delete"' in response.text


def test_dismiss_doc_success(app_client, db_session):
    # Create another batch to keep triage non-empty
    other_batch = IngestBatch(
        subject="Other Batch", source_type=IngestBatchSourceType.EMAIL
    )
    db_session.add(other_batch)
    db_session.commit()
    other_doc = Document(title="Other Doc", ingest_batch_id=other_batch.id)
    db_session.add(other_doc)
    db_session.commit()
    # Create a document that needs triage (no batch for this test)
    doc = Document(
        title="Individual Test Doc",
        pipeline_state=PipelineState.PENDING,
        significance_tier="significant",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    response = app_client.post(f"/triage/dismiss?doc_id={doc.id}")

    assert response.status_code == 200
    assert f'id="triage-row-doc-{doc.id}"' in response.text
    assert 'hx-swap-oob="delete"' in response.text


def test_dismiss_not_found(app_client, db_session):
    response = app_client.post("/triage/dismiss?batch_id=999999")
    assert response.status_code == 404
