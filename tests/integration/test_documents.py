import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, CaseStatus, Document, DocumentPipelineStage

client = TestClient(app)


def _set_stages(db, doc, stages: dict):
    """Insert DocumentPipelineStage rows for a doc that has already been flushed."""
    from datetime import datetime

    def _dt(v):
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    for stage_key, stage_data in stages.items():
        if not isinstance(stage_data, dict):
            continue
        db.add(
            DocumentPipelineStage(
                document_id=doc.id,
                stage=stage_key,
                status=stage_data.get("status", "pending"),
                started_at=_dt(stage_data.get("started_at")),
                completed_at=_dt(stage_data.get("completed_at")),
                error=stage_data.get("error"),
                reason=stage_data.get("reason"),
                attempt=stage_data.get("attempt"),
                max_attempts=stage_data.get("max_attempts"),
                next_at=_dt(stage_data.get("next_at")),
            )
        )
    db.flush()


@pytest.mark.integration
def test_upload_document_to_case(db_session, mock_dispatch_task):
    case = Case(id="UPLOAD-001", title="Upload Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    file_content = b"This is test content"
    response = client.post(
        "/upload",
        files=[("files", ("test_doc.txt", file_content, "text/plain"))],
        data={"case_id": "UPLOAD-001"},
    )

    assert response.status_code in [200, 302, 303]

    doc = db_session.query(Document).filter(Document.case_id == "UPLOAD-001").first()
    assert doc is not None
    assert doc.title is not None

    # Wiring intact: the endpoint queued exactly one background pipeline run...
    mock_dispatch_task.assert_called_once()
    # ...but it must NOT have run synchronously. If the pipeline body executed on
    # the request/daemon thread it would have populated content — a flake source.
    assert doc.content is None


@pytest.mark.integration
def test_upload_document_to_triage(db_session, mock_dispatch_task):
    file_content = b"%PDF-1.4 test"
    response = client.post(
        "/upload",
        files=[("files", ("random_doc.pdf", file_content, "application/pdf"))],
    )
    assert response.status_code in [200, 302, 303]
    assert "queued for processing" in response.text
    # The pipeline is queued, not run inline — no daemon thread racing the test.
    mock_dispatch_task.assert_called_once()


@pytest.mark.integration
def test_upload_auto_case_matching(db_session, mock_dispatch_task):
    file_content = b"%PDF-1.4 test"
    response = client.post(
        "/upload",
        files=[("files", ("ADV-999-Z_Doc.pdf", file_content, "application/pdf"))],
    )

    assert response.status_code in [200, 302, 303]
    assert "queued for processing" in response.text
    mock_dispatch_task.assert_called_once()


@pytest.mark.integration
def test_upload_status_returns_inflight_row_with_polling(db_session):
    from app.models.enums import PipelineState, StageStatus

    doc = Document(
        title="In-flight",
        pipeline_state=PipelineState.RUNNING,
    )
    db_session.add(doc)
    db_session.flush()
    _set_stages(
        db_session,
        doc,
        {
            "extract": {"status": StageStatus.RUNNING.value},
            "metadata": {"status": StageStatus.PENDING.value},
        },
    )
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
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/upload/status/{doc.id}")
    assert response.status_code == 200
    # Terminal rows must not keep polling
    assert "hx-trigger" not in response.text
    assert "ready" in response.text


@pytest.mark.integration
def test_upload_status_retrying_keeps_polling_and_shows_attempt(db_session):
    """A doc with a RETRYING stage must keep polling and show the retry context.

    Regression: before the RETRYING stage status existed, a stage hit `mark_failed`
    before `self.retry()`, which flipped pipeline_state to FAILED and disarmed
    the polling — the next attempt then ran invisibly until the user refreshed.
    """
    from app.models.enums import PipelineState, StageStatus

    doc = Document(
        title="Retrying",
        pipeline_state=PipelineState.RUNNING,
    )
    db_session.add(doc)
    db_session.flush()
    _set_stages(
        db_session,
        doc,
        {
            "extract": {
                "status": StageStatus.RETRYING.value,
                "error": "boom",
                "attempt": 2,
                "max_attempts": 3,
                "next_at": "2026-05-06T18:32:11+00:00",
            },
            "metadata": {"status": StageStatus.PENDING.value},
        },
    )
    db_session.commit()

    response = client.get(f"/upload/status/{doc.id}")
    assert response.status_code == 200
    body = response.text
    # Polling must stay armed during retry — this is the whole point.
    assert f"/upload/status/{doc.id}" in body
    assert 'hx-trigger="every 2s"' in body
    # Surface the retry context (stage + attempt counter).
    assert "retrying" in body.lower()
    assert "2/3" in body


@pytest.mark.integration
def test_upload_status_failed_shows_error(db_session):
    from app.models.enums import PipelineState, StageStatus

    doc = Document(
        title="Borked",
        pipeline_state=PipelineState.FAILED,
    )
    db_session.add(doc)
    db_session.flush()
    _set_stages(
        db_session,
        doc,
        {
            "extract": {
                "status": StageStatus.FAILED.value,
                "error": "Docling crashed mid-page",
            },
        },
    )
    db_session.commit()

    response = client.get(f"/upload/status/{doc.id}")
    assert response.status_code == 200
    body = response.text
    assert "extract failed" in body
    assert "Docling crashed" in body
    assert "hx-trigger" not in body
