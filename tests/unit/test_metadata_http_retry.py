"""METADATA retries transient AI HTTP errors (5xx) instead of failing at once."""

import httpx
import pytest

from app.models.enums import PipelineStage, StageStatus
from app.services.intelligence._ai_call import (
    _parse_litellm_error_code,
    _parse_litellm_error_summary,
    is_transient_ai_http_error,
)


def _status_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://lm/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"HTTP {status}" + (f" {body}" if body else ""),
        request=req,
        response=httpx.Response(status, request=req),
    )


@pytest.mark.unit
def test_parsers_handle_lmstudio_string_error_body():
    body = b'{"error": "Model unloaded."}'
    assert _parse_litellm_error_summary(body) == "Model unloaded."
    assert _parse_litellm_error_code(body) is None


@pytest.mark.unit
def test_parsers_keep_litellm_dict_envelope_and_garbage():
    body = b'{"error": {"message": "boom", "type": "BadRequestError", "code": "x"}}'
    assert _parse_litellm_error_summary(body) == "BadRequestError: boom"
    assert _parse_litellm_error_code(body) == "x"
    assert _parse_litellm_error_summary(b"<html>") is None
    assert _parse_litellm_error_code(b"<html>") is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (500, "", True),
        (503, "", True),
        (400, "", False),
        (400, "Model unloaded", True),
        (401, "", False),
    ],
)
def test_is_transient_ai_http_error(status, body, expected):
    assert is_transient_ai_http_error(_status_error(status, body)) is expected


def _make_doc(db_session, case_id: str):
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_completed, mark_started

    db_session.add(
        Case(
            id=case_id,
            title="T",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    db_session.commit()
    doc = Document(
        title="x", content="x", case_id=case_id, originator_type=OriginatorType.COURT
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()
    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    mark_completed(doc.id, PipelineStage.EXTRACT, db_session)
    db_session.commit()
    return doc.id


def _metadata_status(db_session, doc_id: int) -> str:
    from app.models.database import Document
    from app.services.pipeline_status import stages_dict

    db_session.expire_all()
    doc = db_session.query(Document).filter(Document.id == doc_id).first()
    return stages_dict(doc)[PipelineStage.METADATA.value]["status"]


@pytest.mark.unit
def test_metadata_retries_http_500_then_completes(db_session, monkeypatch):
    doc_id = _make_doc(db_session, "_MR1")
    calls = []

    def _flaky(doc_id, db):
        calls.append(doc_id)
        if len(calls) == 1:
            raise _status_error(500)

    monkeypatch.setattr("app.services.ai_summary._summarize_document_sync", _flaky)
    monkeypatch.setattr("app.tasks.document_processing.time.sleep", lambda s: None)

    from app.tasks.document_processing import _run_phase1_summary

    _run_phase1_summary(doc_id)

    assert len(calls) == 2
    assert _metadata_status(db_session, doc_id) == StageStatus.COMPLETED.value


@pytest.mark.unit
def test_metadata_http_401_fails_without_retry(db_session, monkeypatch):
    doc_id = _make_doc(db_session, "_MR2")
    calls = []

    def _unauthorized(doc_id, db):
        calls.append(doc_id)
        raise _status_error(401)

    monkeypatch.setattr(
        "app.services.ai_summary._summarize_document_sync", _unauthorized
    )
    monkeypatch.setattr("app.tasks.document_processing.time.sleep", lambda s: None)

    from app.tasks.document_processing import _run_phase1_summary

    _run_phase1_summary(doc_id)

    assert len(calls) == 1
    assert _metadata_status(db_session, doc_id) == StageStatus.FAILED.value
