import pytest

from app.models.database import Document, IngestBatch, User
from app.models.enums import IngestBatchSourceType
from app.services.document_service import DocumentService


@pytest.mark.integration
def test_delete_document_updates_triage_feed(app_client, db_session):
    """Deleting through the v1 API removes the doc; the feed reflects it."""
    admin_id = db_session.query(User).filter_by(email="admin@localhost").one().id
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="Triage Delete Test",
        owner_id=admin_id,
    )
    db_session.add(batch)
    db_session.commit()
    doc1 = Document(
        title="Doc 1", ingest_batch_id=batch.id, case_id="_TRIAGE", owner_id=admin_id
    )
    doc2 = Document(
        title="Doc 2", ingest_batch_id=batch.id, case_id="_TRIAGE", owner_id=admin_id
    )
    db_session.add_all([doc1, doc2])
    db_session.commit()

    assert app_client.delete(f"/api/v1/documents/{doc1.id}").status_code == 204
    feed = app_client.get("/api/v1/triage").json()
    (bundle,) = feed["bundles"]
    assert bundle["doc_count"] == 1

    assert app_client.delete(f"/api/v1/documents/{doc2.id}").status_code == 204
    assert app_client.get("/api/v1/triage").json()["bundles"] == []


@pytest.mark.integration
def test_delete_document_last_in_bundle_not_last_in_queue(app_client, db_session):
    admin_id = db_session.query(User).filter_by(email="admin@localhost").one().id
    batch1 = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL, subject="Batch 1", owner_id=admin_id
    )
    batch2 = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL, subject="Batch 2", owner_id=admin_id
    )
    db_session.add_all([batch1, batch2])
    db_session.commit()
    doc1 = Document(
        title="Doc 1", ingest_batch_id=batch1.id, case_id="_TRIAGE", owner_id=admin_id
    )
    doc2 = Document(
        title="Doc 2", ingest_batch_id=batch2.id, case_id="_TRIAGE", owner_id=admin_id
    )
    db_session.add_all([doc1, doc2])
    db_session.commit()

    assert app_client.delete(f"/api/v1/documents/{doc1.id}").status_code == 204
    keys = [b["key"] for b in app_client.get("/api/v1/triage").json()["bundles"]]
    assert keys == [f"batch-{batch2.id}"]


@pytest.mark.integration
def test_delete_document_removes_relative_file(db_session, isolate_data_dir):
    """delete_document must resolve a relative file_path under DATA_DIR and unlink it."""
    triage = isolate_data_dir / "_TRIAGE"
    triage.mkdir(exist_ok=True)
    pdf = triage / "to_delete.pdf"
    pdf.write_bytes(b"%PDF-1.4 delete me")

    doc = Document(
        title="Relative Delete Doc",
        case_id="_TRIAGE",
        file_path="_TRIAGE/to_delete.pdf",
    )
    db_session.add(doc)
    db_session.commit()

    assert DocumentService(db_session).delete_document(doc.id) is True
    assert not pdf.exists()
