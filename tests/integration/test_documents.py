import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

from app.models.database import Case, CaseStatus


@pytest.mark.integration
def test_upload_document_to_case(db_session):
    # Setup: Ensure a case exists
    case = Case(id="UPLOAD-001", title="Upload Test Case", status=CaseStatus.ACTIVE)
    db_session.add(case)
    db_session.commit()

    # Action: Upload a file
    file_content = b"This is a test PDF content."
    response = client.post(
        "/upload",
        files=[("files", ("test_doc.pdf", file_content, "application/pdf"))],
        data={"case_id": "UPLOAD-001"},
    )

    # Check response (usually redirects to the document or case page)
    assert response.status_code in [200, 302, 303]

    # Verify document exists in Case page
    resp = client.get("/cases/UPLOAD-001")
    assert "test_doc.pdf" in resp.text


@pytest.mark.integration
def test_upload_document_to_triage():
    # Action: Upload a file with NO case_id and NO case_id in filename
    file_content = b"Content with no case ID."
    response = client.post(
        "/upload",
        files=[("files", ("random_document.pdf", file_content, "application/pdf"))],
    )
    assert response.status_code in [200, 302, 303]

    # Verify it appears in Triage
    resp = client.get("/triage")
    assert "random_document.pdf" in resp.text


@pytest.mark.integration
def test_upload_auto_case_matching():
    # Action: Upload a file with case ID in filename
    file_content = b"Content for automatic matching."
    response = client.post(
        "/upload",
        files=[("files", ("ADV-999-Z_Document.pdf", file_content, "application/pdf"))],
    )

    assert response.status_code in [200, 302, 303]
    # Check if redirect or text contains the case ID
    # After upload, it might redirect to /activity or the case page
    resp_get = client.get("/activity")
    assert "ADV-999-Z" in resp_get.text
