from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus, PipelineState


def _make_triage_doc(db_session, batch=None, *, title="Doc"):
    doc = Document(
        title=title,
        ingest_batch_id=batch.id if batch else None,
        pipeline_state=PipelineState.PENDING,
        significance_tier="significant",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


def test_delete_endpoint_returns_oob_swap(app_client, db_session):
    # Create a second bundle so triage isn't empty after deletion
    other_batch = IngestBatch(
        subject="Other Batch", source_type=IngestBatchSourceType.EMAIL
    )
    db_session.add(other_batch)
    db_session.commit()
    _make_triage_doc(db_session, other_batch, title="Other Doc")

    batch = IngestBatch(
        subject="Delete Target", source_type=IngestBatchSourceType.EMAIL
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    doc = _make_triage_doc(db_session, batch, title="Target Doc")
    doc_id = doc.id
    batch_id = batch.id

    response = app_client.post(f"/triage/delete?batch_id={batch_id}")

    assert response.status_code == 200
    assert f'id="triage-row-batch-{batch_id}"' in response.text
    assert 'hx-swap-oob="delete"' in response.text

    # Route used a different session — drop our identity-map cache.
    db_session.expire_all()
    assert db_session.get(IngestBatch, batch_id) is None
    assert db_session.get(Document, doc_id) is None
    # Other bundle untouched
    assert db_session.get(IngestBatch, other_batch.id) is not None


def test_delete_last_bundle_returns_empty_state(app_client, db_session):
    batch = IngestBatch(subject="Only One", source_type=IngestBatchSourceType.EMAIL)
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    _make_triage_doc(db_session, batch, title="Sole Doc")

    response = app_client.post(f"/triage/delete?batch_id={batch.id}")

    assert response.status_code == 200
    # Empty state re-renders the feed partial
    assert 'id="triage-feed"' in response.text


def test_delete_404_unknown_batch(app_client):
    response = app_client.post("/triage/delete?batch_id=999999")
    assert response.status_code == 404


def test_delete_409_processing_batch(app_client, db_session):
    batch = IngestBatch(
        subject="Mid-flight",
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    _make_triage_doc(db_session, batch, title="Locked Doc")

    response = app_client.post(f"/triage/delete?batch_id={batch.id}")
    assert response.status_code == 409
    # Batch still present
    assert db_session.get(IngestBatch, batch.id) is not None


def test_delete_loose_doc_via_doc_id(app_client, db_session):
    # Keep triage non-empty
    other_batch = IngestBatch(subject="Other", source_type=IngestBatchSourceType.EMAIL)
    db_session.add(other_batch)
    db_session.commit()
    _make_triage_doc(db_session, other_batch, title="Other")

    doc = _make_triage_doc(db_session, batch=None, title="Loose Doc")
    doc_id = doc.id

    response = app_client.post(f"/triage/delete?doc_id={doc_id}")
    assert response.status_code == 200
    assert f'id="triage-row-doc-{doc_id}"' in response.text
    assert 'hx-swap-oob="delete"' in response.text
    db_session.expire_all()
    assert db_session.get(Document, doc_id) is None
