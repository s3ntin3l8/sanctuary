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
    assert "queued for processing" in response.text


@pytest.mark.integration
def test_upload_auto_case_matching(db_session):
    file_content = b"%PDF-1.4 test"
    response = client.post(
        "/api/v1/upload",
        files=[("files", ("ADV-999-Z_Doc.pdf", file_content, "application/pdf"))],
    )

    assert response.status_code in [200, 302, 303]
    assert "queued for processing" in response.text


@pytest.mark.integration
def test_upload_status_returns_inflight_row_with_polling(db_session):
    from app.models.enums import PipelineState, StageStatus

    doc = Document(
        title="In-flight",
        pipeline_state=PipelineState.RUNNING,
        pipeline_stages={
            "extract": {"status": StageStatus.RUNNING.value},
            "metadata": {"status": StageStatus.PENDING.value},
        },
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/upload/status/{doc.id}")
    assert response.status_code == 200
    body = response.text
    # Self-replacing polling probe is wired
    assert f"/upload/status/{doc.id}" in body
    assert 'hx-trigger="every 2s"' in body
    # Surface the running stage label
    assert "extract" in body


@pytest.mark.integration
def test_upload_status_terminal_drops_polling(db_session):
    from app.models.enums import PipelineState

    doc = Document(
        title="Done",
        pipeline_state=PipelineState.COMPLETED,
        pipeline_stages={},
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/upload/status/{doc.id}")
    assert response.status_code == 200
    # Terminal rows must not keep polling
    assert "hx-trigger" not in response.text
    assert "ready" in response.text


@pytest.mark.integration
def test_upload_status_failed_shows_error(db_session):
    from app.models.enums import PipelineState, StageStatus

    doc = Document(
        title="Borked",
        pipeline_state=PipelineState.FAILED,
        pipeline_stages={
            "extract": {
                "status": StageStatus.FAILED.value,
                "error": "Docling crashed mid-page",
            },
        },
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/upload/status/{doc.id}")
    assert response.status_code == 200
    body = response.text
    assert "extract failed" in body
    assert "Docling crashed" in body
    assert "hx-trigger" not in body
