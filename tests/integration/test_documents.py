import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, CaseStatus, Document

client = TestClient(app)


@pytest.mark.integration
def test_upload_document_to_case(db_session):
    case = Case(id="UPLOAD-001", title="Upload Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    file_content = b"This is test content"
    response = client.post(
        "/api/v1/upload",
        files=[("files", ("test_doc.txt", file_content, "text/plain"))],
        data={"case_id": "UPLOAD-001"},
    )

    assert response.status_code in [200, 302, 303]

    doc = db_session.query(Document).filter(Document.case_id == "UPLOAD-001").first()
    assert doc is not None
    assert doc.title is not None


@pytest.mark.integration
def test_upload_document_to_triage(db_session):
    file_content = b"%PDF-1.4 test"
    response = client.post(
        "/api/v1/upload",
        files=[("files", ("random_doc.pdf", file_content, "application/pdf"))],
    )
    assert response.status_code in [200, 302, 303]
    assert "✓" in response.text


@pytest.mark.integration
def test_upload_auto_case_matching(db_session):
    file_content = b"%PDF-1.4 test"
    response = client.post(
        "/api/v1/upload",
        files=[("files", ("ADV-999-Z_Doc.pdf", file_content, "application/pdf"))],
    )

    assert response.status_code in [200, 302, 303]
    assert "✓" in response.text
