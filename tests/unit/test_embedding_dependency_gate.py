"""Dependency gate on generate_embedding_task: embeddings wait for metadata."""

from unittest.mock import patch

import pytest

from app.tasks.generate_embedding import generate_embedding_task

# generate_embedding_task dependency gate
# ---------------------------------------------------------------------------


def _set_doc_stages(db, doc, stages: dict) -> None:
    from sqlalchemy import text as _sa_text

    db.execute(
        _sa_text("DELETE FROM document_pipeline_stages WHERE document_id = :id"),
        {"id": doc.id},
    )
    for stage_key, stage_data in stages.items():
        db.execute(
            _sa_text(
                "INSERT INTO document_pipeline_stages (document_id, stage, status) "
                "VALUES (:id, :stage, :status)"
            ),
            {
                "id": doc.id,
                "stage": stage_key,
                "status": stage_data.get("status", "pending"),
            },
        )
    db.expire(doc, ["stage_rows"])


@pytest.mark.unit
def test_embedding_defers_when_metadata_pending(db_session, sample_document):
    """Reproduces the doc-95 screenshot bug: dispatch_batch_retry fired
    EMBEDDINGS in parallel with the head retry, and the worker picked up
    EMBEDDINGS first — it ran while METADATA was still pending, producing
    a stepper showing 'Embeddings completed' before 'Extract' completed.

    Gate must:
    1. NOT call the AI / mark started / mark completed.
    2. Return a "deferred" status.
    3. Leave the stage row in PENDING so claim_stage_for_dispatch picks it
       up again after METADATA finishes."""
    _set_doc_stages(
        db_session,
        sample_document,
        {"metadata": {"status": "pending"}, "embeddings": {"status": "pending"}},
    )

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.embeddings.generate_embedding") as mock_embed,
        patch("app.services.pipeline_status.mark_started") as mock_started,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session
        result = generate_embedding_task.run(sample_document.id)

    assert result["status"] == "deferred"
    assert result["reason"] == "metadata_not_completed"
    mock_embed.assert_not_called()
    mock_started.assert_not_called()

    # Stage row stays PENDING so the next dispatch (after METADATA finishes)
    # can claim it via claim_stage_for_dispatch.
    from sqlalchemy import text

    row = db_session.execute(
        text(
            "SELECT status FROM document_pipeline_stages "
            "WHERE document_id = :id AND stage = 'embeddings'"
        ),
        {"id": sample_document.id},
    ).fetchone()
    assert row[0] == "pending"


@pytest.mark.unit
def test_embedding_runs_when_metadata_completed(db_session, sample_document):
    """Happy path: gate passes when METADATA is terminal."""
    _set_doc_stages(
        db_session,
        sample_document,
        {"metadata": {"status": "completed"}, "embeddings": {"status": "pending"}},
    )

    async def _ok(_doc_id):
        return None

    with (
        patch("app.dependencies.get_db_session") as mock_get_db,
        patch("app.services.embeddings.generate_embedding", side_effect=_ok),
        patch("app.services.pipeline_status.mark_started"),
        patch("app.services.pipeline_status.mark_completed"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db.return_value = db_session
        result = generate_embedding_task.run(sample_document.id)

    assert result["status"] == "success"
