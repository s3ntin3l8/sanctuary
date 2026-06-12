"""Integration tests for document routes — original-file serving."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document, DocumentPipelineStage
from app.models.enums import PipelineStage, StageStatus
from app.services.pipeline_status import stages_dict

client = TestClient(app)


@pytest.mark.integration
def test_open_original_returns_file(db_session, isolate_data_dir):
    """GET /document/:id/original serves the file as octet-stream."""
    # Write a real temp file inside the isolated data dir
    tmp = isolate_data_dir / "test_doc.pdf"
    tmp.write_bytes(b"%PDF-1.4 fake pdf content")

    doc = Document(title="Original File Doc", file_path=str(tmp))
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/document/{doc.id}/original")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        ("application/pdf", "application/octet-stream")
    )
    assert b"%PDF" in response.content


@pytest.mark.integration
def test_open_original_404_missing_file(db_session, isolate_data_dir):
    """GET /document/:id/original returns 404 when file_path points to non-existent file."""
    doc = Document(title="Missing File Doc", file_path="/nonexistent/path/file.pdf")
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/document/{doc.id}/original")
    assert response.status_code == 404


@pytest.mark.integration
def test_open_original_404_no_file_path(db_session):
    """GET /document/:id/original returns 404 when file_path is None."""
    doc = Document(title="No File Path Doc", file_path=None)
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/document/{doc.id}/original")
    assert response.status_code == 404


@pytest.mark.integration
def test_open_original_404_nonexistent_doc(db_session):
    """GET /document/:id/original returns 404 for a non-existent document id."""
    response = client.get("/document/999999/original")
    assert response.status_code == 404


@pytest.mark.integration
def test_open_original_serves_relative_path(db_session, isolate_data_dir):
    """Regression: file_path stored RELATIVE to DATA_DIR must serve (the HUD 404 bug).

    Documents persist file_path relative to DATA_DIR so the DB is portable across
    hosts. The route must re-root it under the running host's DATA_DIR.
    """
    triage = isolate_data_dir / "_TRIAGE"
    triage.mkdir(exist_ok=True)
    (triage / "letter.pdf").write_bytes(b"%PDF-1.4 relative path content")

    doc = Document(title="Relative Path Doc", file_path="_TRIAGE/letter.pdf")
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/document/{doc.id}/original")
    assert response.status_code == 200
    assert b"%PDF" in response.content


@pytest.mark.integration
def test_ingested_attachment_is_stored_relative_and_served(
    db_session, isolate_data_dir
):
    """End-to-end: ingest an email PDF, confirm relative storage, serve it via the route."""
    import email.message
    from pathlib import Path

    from app.models.enums import IngestBatchSourceType
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["Subject"] = "Schriftsatz ADV-024-A"
    msg["Message-ID"] = "<rel-path-001@example.com>"
    msg.set_content("im Anhang")
    msg.add_attachment(
        b"%PDF-1.4 attached letter",
        maintype="application",
        subtype="pdf",
        filename="schriftsatz.pdf",
    )

    batch = ingest_raw_email(
        db_session, msg.as_bytes(), source_type=IngestBatchSourceType.EMAIL
    )
    doc = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).one()

    # Stored path is relative — not baked to this host's absolute layout.
    assert not Path(doc.file_path).is_absolute()
    assert (isolate_data_dir / doc.file_path).exists()

    response = client.get(f"/document/{doc.id}/original")
    assert response.status_code == 200
    assert b"%PDF" in response.content


# ── Pipeline retry-all ────────────────────────────────────────────────────


def _doc_with_stages(db, **overrides) -> Document:
    """Create a doc whose pipeline_stages start out completed/failed."""
    stages = {s.value: {"status": StageStatus.COMPLETED.value} for s in PipelineStage}
    stages.update(overrides)
    doc = Document(title="Pipeline Doc")
    db.add(doc)
    db.flush()
    for stage_key, stage_data in stages.items():
        db.add(
            DocumentPipelineStage(
                document_id=doc.id,
                stage=stage_key,
                status=stage_data["status"],
                error=stage_data.get("error"),
                reason=stage_data.get("reason"),
            )
        )
    db.commit()
    db.refresh(doc)
    return doc


@pytest.mark.integration
def test_retry_all_resets_stages_and_dispatches(db_session):
    doc = _doc_with_stages(
        db_session,
        extract={"status": StageStatus.FAILED.value, "error": "boom"},
    )
    with patch("app.api.documents.dispatch_pipeline_retry") as mock_dispatch:
        response = client.post(f"/document/{doc.id}/pipeline/retry-all")

    assert response.status_code == 200
    db_session.refresh(doc)
    # Every non-skipped stage is now PENDING with cleared error/timestamps.
    for record in stages_dict(doc).values():
        assert record["status"] == StageStatus.PENDING.value
        assert record.get("error") is None
    mock_dispatch.assert_called_once()
    # Dispatched stage is EXTRACT — kicks off the cascade.
    assert mock_dispatch.call_args[0][2] == PipelineStage.EXTRACT


@pytest.mark.integration
def test_retry_all_preserves_skipped(db_session):
    doc = _doc_with_stages(
        db_session,
        batch_analysis={"status": StageStatus.SKIPPED.value, "reason": "manual upload"},
    )
    with patch("app.api.documents.dispatch_pipeline_retry"):
        response = client.post(f"/document/{doc.id}/pipeline/retry-all")

    assert response.status_code == 200
    db_session.refresh(doc)
    assert stages_dict(doc)["batch_analysis"]["status"] == StageStatus.SKIPPED.value


@pytest.mark.integration
def test_retry_all_409_when_running(db_session):
    doc = _doc_with_stages(db_session, enrich={"status": StageStatus.RUNNING.value})
    with patch("app.api.documents.dispatch_pipeline_retry") as mock_dispatch:
        response = client.post(f"/document/{doc.id}/pipeline/retry-all")

    assert response.status_code == 409
    assert b"still running" in response.content
    db_session.refresh(doc)
    assert stages_dict(doc)["enrich"]["status"] == StageStatus.RUNNING.value
    mock_dispatch.assert_not_called()


@pytest.mark.integration
def test_retry_all_404_for_unknown_doc(db_session):
    response = client.post("/document/999999/pipeline/retry-all")
    assert response.status_code == 404
