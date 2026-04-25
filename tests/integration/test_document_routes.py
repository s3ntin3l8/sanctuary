"""Integration tests for document routes — original-file serving."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document

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
    assert response.headers["content-type"].startswith("application/octet-stream")
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
