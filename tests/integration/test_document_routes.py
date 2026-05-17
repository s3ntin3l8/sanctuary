"""Integration tests for document routes — original-file serving."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document
from app.models.enums import PipelineStage, StageStatus

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


# ── Pipeline retry-all ────────────────────────────────────────────────────


def _doc_with_stages(db, **overrides) -> Document:
    """Create a doc whose pipeline_stages start out completed/failed."""
    stages = {s.value: {"status": StageStatus.COMPLETED.value} for s in PipelineStage}
    stages.update(overrides)
    doc = Document(title="Pipeline Doc", pipeline_stages=stages)
    db.add(doc)
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
    for record in doc.pipeline_stages.values():
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
    assert doc.pipeline_stages["batch_analysis"]["status"] == StageStatus.SKIPPED.value


@pytest.mark.integration
def test_retry_all_409_when_running(db_session):
    doc = _doc_with_stages(db_session, enrich={"status": StageStatus.RUNNING.value})
    with patch("app.api.documents.dispatch_pipeline_retry") as mock_dispatch:
        response = client.post(f"/document/{doc.id}/pipeline/retry-all")

    assert response.status_code == 409
    assert b"still running" in response.content
    db_session.refresh(doc)
    assert doc.pipeline_stages["enrich"]["status"] == StageStatus.RUNNING.value
    mock_dispatch.assert_not_called()


@pytest.mark.integration
def test_retry_all_404_for_unknown_doc(db_session):
    response = client.post("/document/999999/pipeline/retry-all")
    assert response.status_code == 404
