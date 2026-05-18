"""Regression tests for XSS in upload-status row HTML.

External attackers can supply attachment filenames via email ingest. The
upload-status endpoint must HTML-escape the document title and error message
before interpolating into the response HTML.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document, DocumentPipelineStage
from app.models.enums import PipelineState

client = TestClient(app)

PAYLOAD = '<script>alert("xss")</script>'


def test_upload_status_escapes_title_in_completed_row(db_session):
    doc = Document(title=PAYLOAD, pipeline_state=PipelineState.COMPLETED)
    db_session.add(doc)
    db_session.commit()

    body = client.get(f"/upload/status/{doc.id}").text

    assert PAYLOAD not in body, "Raw <script> rendered into response — XSS"
    assert "&lt;script&gt;" in body or "&#x3C;script&#x3E;" in body


def test_upload_status_escapes_title_in_failed_row(db_session):
    doc = Document(
        title=PAYLOAD,
        pipeline_state=PipelineState.FAILED,
    )
    db_session.add(doc)
    db_session.flush()
    db_session.add(
        DocumentPipelineStage(
            document_id=doc.id,
            stage="extract",
            status="failed",
            error="<img src=x onerror=alert(1)>",
        )
    )
    db_session.commit()

    body = client.get(f"/upload/status/{doc.id}").text

    assert PAYLOAD not in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;script&gt;" in body or "&#x3C;script&#x3E;" in body


def test_upload_status_escapes_title_in_inflight_row(db_session):
    doc = Document(title=PAYLOAD, pipeline_state=PipelineState.RUNNING)
    db_session.add(doc)
    db_session.commit()

    body = client.get(f"/upload/status/{doc.id}").text

    assert PAYLOAD not in body
