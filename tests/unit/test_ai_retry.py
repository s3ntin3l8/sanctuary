"""Claims/entities/relationships re-queue on transient AI HTTP errors (#222)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery.exceptions import Retry

from app.models.database import Case, Document
from app.models.enums import (
    CaseStatus,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
    StageStatus,
)
from app.services.pipeline_status import (
    initialize,
    mark_completed,
    mark_started,
    stages_dict,
)
from app.tasks.ai_retry import retry_if_transient_ai_error


def _status_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://lm/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"HTTP {status}" + (f" {body}" if body else ""),
        request=req,
        response=httpx.Response(status, request=req),
    )


def _fake_task(retries: int, max_retries: int = 3) -> MagicMock:
    task = MagicMock()
    task.request.retries = retries
    task.max_retries = max_retries
    task.retry.side_effect = Retry()
    return task


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "retries", "should_retry"),
    [
        (_status_error(500), 0, True),
        (_status_error(503), 2, True),
        (_status_error(400, "Model unloaded"), 0, True),
        (_status_error(500), 3, False),  # retries exhausted
        (_status_error(401), 0, False),
        (_status_error(400), 0, False),
        (ValueError("boom"), 0, False),
        (httpx.ReadTimeout("slow"), 0, False),
    ],
)
def test_helper_retries_only_transient_http_errors(exc, retries, should_retry):
    task = _fake_task(retries)
    with (
        patch("app.tasks.ai_retry.get_db_session"),
        patch("app.tasks.ai_retry.schedule_retry") as sched,
    ):
        if should_retry:
            with pytest.raises(Retry):
                retry_if_transient_ai_error(task, 1, PipelineStage.CLAIMS, exc)
            task.retry.assert_called_once_with(exc=exc, countdown=60 * (retries + 1))
            sched.assert_called_once()
            assert sched.call_args.kwargs["attempt"] == retries + 1
        else:
            retry_if_transient_ai_error(task, 1, PipelineStage.CLAIMS, exc)
            task.retry.assert_not_called()
            sched.assert_not_called()


# stage -> (task import path, extract() patch target, extra patches for failure path)
_TASKS = {
    PipelineStage.CLAIMS: (
        "app.tasks.extract_claims.extract_claims_task",
        "app.services.intelligence.claim_extractor.extract",
        ["app.services.intelligence.orchestrator.trigger_case_brief_if_ready"],
    ),
    PipelineStage.ENTITIES: (
        "app.tasks.extract_entities.extract_entities_task",
        "app.services.intelligence.entity_extractor.extract",
        [],
    ),
    PipelineStage.RELATIONSHIPS: (
        "app.tasks.detect_relationships.detect_relationships_task",
        "app.services.intelligence.relationship_detector.detect",
        [],
    ),
}


@pytest.fixture
def enriched_doc_id(db_session):
    db_session.add(
        Case(
            id="_AR1",
            title="T",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    db_session.commit()
    doc = Document(
        title="x",
        content="x",
        case_id="_AR1",
        originator_type=OriginatorType.COURT,
        ai_summary_created_at=datetime.now(UTC),
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()
    mark_started(doc.id, PipelineStage.ENRICH, db_session)
    mark_completed(doc.id, PipelineStage.ENRICH, db_session)
    db_session.commit()
    return doc.id


def _stage(db_session, doc_id: int, stage: PipelineStage) -> dict:
    db_session.expire_all()
    doc = db_session.query(Document).filter(Document.id == doc_id).first()
    return stages_dict(doc)[stage.value]


@pytest.mark.unit
@pytest.mark.parametrize("stage", list(_TASKS))
def test_task_requeues_on_http_500(db_session, enriched_doc_id, stage):
    from importlib import import_module

    task_path, extract_path, extra = _TASKS[stage]
    mod, name = task_path.rsplit(".", 1)
    task = getattr(import_module(mod), name)

    with patch(extract_path, side_effect=_status_error(500)):
        # Called directly (no worker), Celery's retry() re-raises the original.
        with pytest.raises(httpx.HTTPStatusError):
            task(enriched_doc_id)

    st = _stage(db_session, enriched_doc_id, stage)
    assert st["status"] == StageStatus.RETRYING.value
    assert "500" in st["error"]


@pytest.mark.unit
@pytest.mark.parametrize("stage", list(_TASKS))
def test_task_fails_without_retry_on_http_401(db_session, enriched_doc_id, stage):
    from contextlib import ExitStack
    from importlib import import_module

    task_path, extract_path, extra = _TASKS[stage]
    mod, name = task_path.rsplit(".", 1)
    task = getattr(import_module(mod), name)

    with ExitStack() as stack:
        for target in extra:
            stack.enter_context(patch(target))
        stack.enter_context(patch(extract_path, side_effect=_status_error(401)))
        result = task(enriched_doc_id)

    assert result["status"] == "failed"
    assert _stage(db_session, enriched_doc_id, stage)["status"] == (
        StageStatus.FAILED.value
    )
