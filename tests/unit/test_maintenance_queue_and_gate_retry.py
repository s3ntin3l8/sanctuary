"""Queue routing for the recovery sweep (#144) and gate-timeout retries (#143)."""

from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from sqlalchemy import text

from app.models.database import Document
from app.models.enums import PipelineStage
from app.services.model_gate import ModelGateTimeout
from app.services.pipeline_status import initialize
from app.tasks.celery_app import MAINTENANCE_QUEUE, MAINTENANCE_TASKS, celery_app


def _queue_for(task_name: str) -> str:
    return celery_app.amqp.router.route({}, task_name)["queue"].name


@pytest.mark.unit
def test_recovery_sweep_and_housekeeping_run_on_the_maintenance_queue():
    for name in MAINTENANCE_TASKS:
        assert _queue_for(name) == MAINTENANCE_QUEUE


@pytest.mark.unit
def test_every_maintenance_task_is_a_registered_task_and_in_the_beat_schedule():
    celery_app.loader.import_default_modules()
    beat_tasks = {e["task"] for e in celery_app.conf.beat_schedule.values()}
    for name in MAINTENANCE_TASKS:
        assert name in celery_app.tasks, f"{name} is not a registered Celery task"
        assert name in beat_tasks, f"{name} is routed to maintenance but not scheduled"


@pytest.mark.unit
def test_llm_and_ocr_work_does_not_move_to_the_maintenance_queue():
    assert _queue_for("app.tasks.document_processing.process_document_task") == "ingest"
    assert _queue_for("app.tasks.document_processing.metadata_task") == "ai"
    assert (
        _queue_for("app.tasks.maintenance.retry_failed_claim_embeddings_task") == "ai"
    )


@pytest.mark.unit
def test_gate_timeout_is_a_timeouterror_for_existing_handlers():
    assert issubclass(ModelGateTimeout, TimeoutError)


# --- metadata_task retry on a model-gate timeout --------------------------------------------


@pytest.fixture
def doc(db_session, sample_case):
    d = Document(title="D", content="x", case_id=sample_case.id)
    db_session.add(d)
    db_session.flush()
    initialize(d, batched=False, db=db_session)
    db_session.commit()
    db_session.execute(
        text(
            "UPDATE document_pipeline_stages SET status = 'running' "
            "WHERE document_id = :d AND stage = 'metadata'"
        ),
        {"d": d.id},
    )
    db_session.commit()
    return d


def _stage(db, doc_id, stage):
    db.expire_all()
    return db.execute(
        text(
            "SELECT status, error FROM document_pipeline_stages "
            "WHERE document_id = :d AND stage = :s"
        ),
        {"d": doc_id, "s": stage},
    ).one()


def _fake_task(retries: int, max_retries: int = 4):
    task = MagicMock()
    task.request.retries = retries
    task.max_retries = max_retries
    task.retry.side_effect = Retry()
    return task


@pytest.mark.unit
def test_gate_timeout_schedules_a_retry_instead_of_failing_the_doc(db_session, doc):
    from app.tasks.document_processing import _retry_or_fail_after_gate_timeout

    task = _fake_task(retries=0)
    with (
        patch("app.tasks.document_processing.get_db_session", lambda: db_session),
        patch.object(db_session, "close"),
        pytest.raises(Retry),
    ):
        _retry_or_fail_after_gate_timeout(task, doc.id, ModelGateTimeout("busy"))

    status, error = _stage(db_session, doc.id, "metadata")
    assert status == "retrying"
    assert "busy" in error
    assert _stage(db_session, doc.id, "enrich")[0] == "pending"  # not cascaded
    task.retry.assert_called_once()
    assert task.retry.call_args.kwargs["countdown"] > 0


@pytest.mark.unit
def test_gate_timeout_fails_and_cascades_once_retries_are_spent(db_session, doc):
    from app.tasks.document_processing import _retry_or_fail_after_gate_timeout

    task = _fake_task(retries=4, max_retries=4)
    with (
        patch("app.tasks.document_processing.get_db_session", lambda: db_session),
        patch.object(db_session, "close"),
    ):
        _retry_or_fail_after_gate_timeout(task, doc.id, ModelGateTimeout("busy"))

    assert _stage(db_session, doc.id, "metadata")[0] == "failed"
    assert _stage(db_session, doc.id, "enrich")[0] == "failed"
    task.retry.assert_not_called()


@pytest.mark.unit
def test_metadata_task_first_delivery_claims_the_stage_but_a_retry_does_not(
    db_session, doc
):
    """A Celery retry continues the task's own claim: the stage is RETRYING, which
    the pending->running CAS would reject (and wrongly skip the retry)."""
    from app.tasks import document_processing as dp

    db_session.execute(
        text(
            "UPDATE document_pipeline_stages SET status = 'retrying' "
            "WHERE document_id = :d AND stage = 'metadata'"
        ),
        {"d": doc.id},
    )
    db_session.commit()

    with (
        patch.object(dp, "get_db_session", lambda: db_session),
        patch.object(db_session, "close"),
        patch.object(dp, "_run_phase1_summary") as run,
        patch(
            "app.services.pipeline_status.claim_stage_for_dispatch",
            return_value=False,
        ) as claim,
        patch.object(dp.metadata_task, "request_stack") as stack,
    ):
        stack.top.request.retries = 1
        dp.metadata_task.run(doc.id)

    # Later stages (embeddings, ...) are claimed as usual; METADATA must not be.
    claimed = [c.args[1] for c in claim.call_args_list]
    assert PipelineStage.METADATA not in claimed
    run.assert_called_once_with(doc.id)
