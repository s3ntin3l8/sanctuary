"""Tests for app.tasks.dispatch — specifically the dispatch failure surfacer."""

from unittest.mock import MagicMock, patch

import pytest

from app.models.enums import PipelineStage
from app.tasks.dispatch import _record_dispatch_failure


@pytest.mark.unit
def test_dispatch_failure_marks_metadata_stage_when_extract_completed():
    """For process_document_task (ambiguous retry_task), the recorder picks
    the first non-terminal stage of the doc. EXTRACT completed → METADATA."""
    fake_db = MagicMock()
    fake_doc = MagicMock()
    fake_stages = {
        "extract": {"status": "completed"},
        "metadata": {"status": "pending"},
    }
    fake_db.query.return_value.filter.return_value.first.return_value = fake_doc

    with (
        patch("app.dependencies.get_db_session", return_value=fake_db),
        patch("app.services.pipeline_status.mark_failed_with_cascade") as mock_mfc,
        patch("app.services.pipeline_status.stages_dict", return_value=fake_stages),
    ):
        _record_dispatch_failure(
            "app.tasks.document_processing.process_document_task",
            (42,),
            RuntimeError("database is locked"),
        )

    assert mock_mfc.called
    call_args = mock_mfc.call_args
    assert call_args.args[0] == 42
    assert call_args.args[1] == PipelineStage.METADATA
    assert "dispatch error" in call_args.kwargs["error"]
    assert "database is locked" in call_args.kwargs["error"]
    fake_db.close.assert_called_once()


@pytest.mark.unit
def test_dispatch_failure_marks_extract_stage_when_extract_pending():
    """When EXTRACT is the doc's first non-terminal stage, mark that failed."""
    fake_db = MagicMock()
    fake_doc = MagicMock()
    fake_stages = {
        "extract": {"status": "pending"},
        "metadata": {"status": "pending"},
    }
    fake_db.query.return_value.filter.return_value.first.return_value = fake_doc

    with (
        patch("app.dependencies.get_db_session", return_value=fake_db),
        patch("app.services.pipeline_status.mark_failed_with_cascade") as mock_mfc,
        patch("app.services.pipeline_status.stages_dict", return_value=fake_stages),
    ):
        _record_dispatch_failure(
            "app.tasks.document_processing.process_document_task",
            (42,),
            RuntimeError("boom"),
        )

    assert mock_mfc.called
    assert mock_mfc.call_args.args[1] == PipelineStage.EXTRACT


@pytest.mark.unit
def test_dispatch_failure_marks_embeddings_stage_failed():
    """generate_embedding_task failures map to EMBEDDINGS."""
    fake_db = MagicMock()
    fake_doc = MagicMock()
    fake_stages = {"embeddings": {"status": "pending"}}
    fake_db.query.return_value.filter.return_value.first.return_value = fake_doc

    with (
        patch("app.dependencies.get_db_session", return_value=fake_db),
        patch("app.services.pipeline_status.mark_failed_with_cascade") as mock_mfc,
        patch("app.services.pipeline_status.stages_dict", return_value=fake_stages),
    ):
        _record_dispatch_failure(
            "app.tasks.generate_embedding.generate_embedding_task",
            (99,),
            ValueError("boom"),
        )

    assert mock_mfc.called
    assert mock_mfc.call_args.args[1] == PipelineStage.EMBEDDINGS


@pytest.mark.unit
def test_dispatch_failure_skips_when_stage_already_failed():
    """If the task's own handler already wrote a specific error, the recorder
    must not stomp it with a generic 'dispatch error: ...'."""
    fake_db = MagicMock()
    fake_doc = MagicMock()
    fake_stages = {
        "embeddings": {"status": "failed", "error": "real specific cause"},
    }
    fake_db.query.return_value.filter.return_value.first.return_value = fake_doc

    with (
        patch("app.dependencies.get_db_session", return_value=fake_db),
        patch("app.services.pipeline_status.mark_failed_with_cascade") as mock_mfc,
        patch("app.services.pipeline_status.stages_dict", return_value=fake_stages),
    ):
        _record_dispatch_failure(
            "app.tasks.generate_embedding.generate_embedding_task",
            (99,),
            ValueError("generic propagated"),
        )

    mock_mfc.assert_not_called()


@pytest.mark.unit
def test_dispatch_failure_skips_batch_keyed_tasks():
    """analyze_batch_task takes a batch_id, not doc_id. The recorder must NOT
    treat that integer as a doc_id (would corrupt unrelated docs that happen
    to share the same primary key)."""
    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.pipeline_status.mark_failed_with_cascade") as mock_mfc,
    ):
        _record_dispatch_failure(
            "app.tasks.analyze_batch.analyze_batch_task",
            (3,),  # batch_id=3 — must NOT be treated as doc_id=3
            RuntimeError("boom"),
        )

    mock_get_db.assert_not_called()
    mock_mfc.assert_not_called()


@pytest.mark.unit
def test_dispatch_failure_unknown_label_is_silent():
    """Unrecognised labels exit cleanly without touching the DB."""
    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.pipeline_status.mark_failed_with_cascade") as mock_mfc,
    ):
        _record_dispatch_failure(
            "app.tasks.unknown_module.unknown_task",
            (1,),
            RuntimeError("boom"),
        )

    mock_get_db.assert_not_called()
    mock_mfc.assert_not_called()


@pytest.mark.unit
def test_dispatch_failure_non_int_arg_is_silent():
    """Batch-id dispatches (str arg) shouldn't try to mark a doc — just exit."""
    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.pipeline_status.mark_failed_with_cascade") as mock_mfc,
    ):
        _record_dispatch_failure(
            "app.tasks.document_processing.process_document_task",
            ("not-an-int",),
            RuntimeError("boom"),
        )

    mock_get_db.assert_not_called()
    mock_mfc.assert_not_called()


@pytest.mark.unit
def test_dispatch_failure_swallows_inner_exceptions():
    """The recorder must not raise even if the DB layer itself blows up."""
    with (
        patch("app.dependencies.get_db_session", side_effect=RuntimeError("db down")),
        patch("app.services.pipeline_status.mark_failed_with_cascade"),
    ):
        # Should not raise.
        _record_dispatch_failure(
            "app.tasks.document_processing.process_document_task",
            (7,),
            RuntimeError("orig"),
        )
