"""Tests for generate_case_brief.py's retry/terminal-failure branches.

Before PR2 (2026-09-27 ingestion audit), case_brief_generator.generate()
swallowed every exception internally and wrote the failure state itself,
which meant these branches — the ReadTimeout-gets-one-retry path and the
generic-Exception-retries-with-backoff path — were dead code: generate()
never raised, so the task's own except clauses never ran. generate() now
re-raises, and these tests exercise the branches directly for the first
time.

Note: self.retry() under request.called_directly (true when a bound task
is invoked via .run() rather than through a real Celery worker) re-raises
the *original* exception rather than celery.exceptions.Retry — so, like
test_enrich_retries_on_transient_4xx_from_lm_studio in test_tasks.py, the
retry path is exercised by patching .retry itself with a side_effect that
raises a real Retry instance, rather than relying on the unpatched method.
"""

from unittest.mock import patch

import httpx
import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded

from app.tasks.generate_case_brief import (
    generate_case_brief_task,
    refresh_case_brief_task,
)


@pytest.mark.unit
def test_generate_case_brief_retries_once_on_timeout_then_marks_failed(
    db_session, sample_case
):
    retry_sentinel = Retry(exc=httpx.ReadTimeout("simulated"))
    with (
        patch(
            "app.services.intelligence.case_brief_generator.generate",
            side_effect=httpx.ReadTimeout("simulated"),
        ),
        patch(
            "app.services.intelligence.case_brief_generator.mark_brief_failed"
        ) as mock_mark_failed,
        patch("app.tasks.generate_case_brief._release_brief_claim"),
        patch.object(
            generate_case_brief_task, "retry", side_effect=retry_sentinel
        ) as mock_retry,
    ):
        # First attempt (retries=0): must retry, not mark failed yet.
        generate_case_brief_task.request.update({"retries": 0})
        try:
            with pytest.raises(Retry):
                generate_case_brief_task.run(sample_case.id)
        finally:
            generate_case_brief_task.request.clear()
        mock_retry.assert_called_once()
        mock_mark_failed.assert_not_called()

        # Second attempt (retries=1): the retry budget (max_retries=1 for
        # this specific httpx.ReadTimeout branch) is exhausted — .retry()
        # itself is never called on this path.
        generate_case_brief_task.request.update({"retries": 1})
        try:
            result = generate_case_brief_task.run(sample_case.id)
        finally:
            generate_case_brief_task.request.clear()

    assert result["status"] == "failed"
    mock_mark_failed.assert_called_once_with(sample_case.id, "timeout: simulated")


@pytest.mark.unit
def test_generate_case_brief_retries_generic_exception_with_backoff_then_fails(
    db_session, sample_case
):
    retry_sentinel = Retry(exc=RuntimeError("boom"))
    with (
        patch(
            "app.services.intelligence.case_brief_generator.generate",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "app.services.intelligence.case_brief_generator.mark_brief_failed"
        ) as mock_mark_failed,
        patch("app.tasks.generate_case_brief._release_brief_claim"),
        patch.object(
            generate_case_brief_task, "retry", side_effect=retry_sentinel
        ) as mock_retry,
    ):
        generate_case_brief_task.request.update({"retries": 0})
        try:
            with pytest.raises(Retry):
                generate_case_brief_task.run(sample_case.id)
        finally:
            generate_case_brief_task.request.clear()
        mock_retry.assert_called_once()
        mock_mark_failed.assert_not_called()

        generate_case_brief_task.request.update(
            {"retries": generate_case_brief_task.max_retries}
        )
        try:
            result = generate_case_brief_task.run(sample_case.id)
        finally:
            generate_case_brief_task.request.clear()

    assert result["status"] == "failed"
    mock_mark_failed.assert_called_once_with(sample_case.id, "boom")


@pytest.mark.unit
def test_generate_case_brief_success_does_not_mark_failed(db_session, sample_case):
    with (
        patch("app.services.intelligence.case_brief_generator.generate"),
        patch(
            "app.services.intelligence.case_brief_generator.mark_brief_failed"
        ) as mock_mark_failed,
        patch("app.tasks.generate_case_brief._release_brief_claim"),
    ):
        result = generate_case_brief_task.run(sample_case.id)

    assert result["status"] == "success"
    mock_mark_failed.assert_not_called()


@pytest.mark.unit
def test_generate_case_brief_soft_time_limit_marks_failed_without_retry(
    db_session, sample_case
):
    """PR3a: unlike every other exception branch, a soft time limit must not
    attempt self.retry() at all — it means "wrap up now," not "try again."""
    with (
        patch(
            "app.services.intelligence.case_brief_generator.generate",
            side_effect=SoftTimeLimitExceeded("simulated"),
        ),
        patch(
            "app.services.intelligence.case_brief_generator.mark_brief_failed"
        ) as mock_mark_failed,
        patch("app.tasks.generate_case_brief._release_brief_claim"),
        patch.object(generate_case_brief_task, "retry") as mock_retry,
    ):
        result = generate_case_brief_task.run(sample_case.id)

    assert result["status"] == "failed"
    mock_retry.assert_not_called()
    mock_mark_failed.assert_called_once_with(
        sample_case.id,
        f"soft time limit exceeded: {SoftTimeLimitExceeded('simulated')}",
    )


@pytest.mark.unit
def test_refresh_case_brief_retries_once_on_timeout_then_marks_failed(
    db_session, sample_case
):
    retry_sentinel = Retry(exc=httpx.ReadTimeout("simulated"))
    with (
        patch(
            "app.services.intelligence.case_brief_generator.generate",
            side_effect=httpx.ReadTimeout("simulated"),
        ),
        patch(
            "app.services.intelligence.case_brief_generator.mark_brief_failed"
        ) as mock_mark_failed,
        patch("app.tasks.generate_case_brief._release_brief_claim"),
        patch.object(
            refresh_case_brief_task, "retry", side_effect=retry_sentinel
        ) as mock_retry,
    ):
        refresh_case_brief_task.request.update({"retries": 0})
        try:
            with pytest.raises(Retry):
                refresh_case_brief_task.run(sample_case.id)
        finally:
            refresh_case_brief_task.request.clear()
        mock_retry.assert_called_once()

        refresh_case_brief_task.request.update({"retries": 1})
        try:
            result = refresh_case_brief_task.run(sample_case.id)
        finally:
            refresh_case_brief_task.request.clear()

    assert result["status"] == "failed"
    mock_mark_failed.assert_called_once_with(sample_case.id, "timeout: simulated")


@pytest.mark.unit
def test_refresh_case_brief_retries_generic_exception_then_fails(
    db_session, sample_case
):
    retry_sentinel = Retry(exc=RuntimeError("boom"))
    with (
        patch(
            "app.services.intelligence.case_brief_generator.generate",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "app.services.intelligence.case_brief_generator.mark_brief_failed"
        ) as mock_mark_failed,
        patch("app.tasks.generate_case_brief._release_brief_claim"),
        patch.object(
            refresh_case_brief_task, "retry", side_effect=retry_sentinel
        ) as mock_retry,
    ):
        refresh_case_brief_task.request.update({"retries": 0})
        try:
            with pytest.raises(Retry):
                refresh_case_brief_task.run(sample_case.id)
        finally:
            refresh_case_brief_task.request.clear()
        mock_retry.assert_called_once()

        refresh_case_brief_task.request.update(
            {"retries": refresh_case_brief_task.max_retries}
        )
        try:
            result = refresh_case_brief_task.run(sample_case.id)
        finally:
            refresh_case_brief_task.request.clear()

    assert result["status"] == "failed"
    mock_mark_failed.assert_called_once_with(sample_case.id, "boom")


@pytest.mark.unit
def test_refresh_case_brief_soft_time_limit_marks_failed_without_retry(
    db_session, sample_case
):
    with (
        patch(
            "app.services.intelligence.case_brief_generator.generate",
            side_effect=SoftTimeLimitExceeded("simulated"),
        ),
        patch(
            "app.services.intelligence.case_brief_generator.mark_brief_failed"
        ) as mock_mark_failed,
        patch("app.tasks.generate_case_brief._release_brief_claim"),
        patch.object(refresh_case_brief_task, "retry") as mock_retry,
    ):
        result = refresh_case_brief_task.run(sample_case.id)

    assert result["status"] == "failed"
    mock_retry.assert_not_called()
    mock_mark_failed.assert_called_once_with(
        sample_case.id,
        f"soft time limit exceeded: {SoftTimeLimitExceeded('simulated')}",
    )


@pytest.mark.unit
def test_mark_brief_failed_writes_failed_status(db_session, sample_case):
    from app.models.database import Case
    from app.models.enums import BriefState
    from app.services.intelligence.case_brief_generator import mark_brief_failed

    sample_case.ai_brief = {"posture": "kept"}
    db_session.commit()

    mark_brief_failed(sample_case.id, "simulated error")

    db_session.expire_all()
    case = db_session.query(Case).filter(Case.id == sample_case.id).first()
    assert case.brief_state == BriefState.FAILED
    assert case.brief_error == "simulated error"
    assert case.ai_brief == {"posture": "kept"}


@pytest.mark.unit
def test_mark_brief_failed_no_op_for_unknown_case():
    """Must not raise when the case doesn't exist (e.g. deleted mid-flight)."""
    from app.services.intelligence.case_brief_generator import mark_brief_failed

    mark_brief_failed("_DOES_NOT_EXIST", "simulated error")
