"""Tests for the pipeline-stepper template's TZ handling and the dependency
gate on generate_embedding_task.

The stepper renders timestamps from `Document.pipeline_stages` which holds
ISO strings (UTC). Without the local_strftime filter accepting strings, the
template was emitting raw UTC slices like "2026-05-22 22:15" while the user
was in Europe/Berlin (UTC+2) and seeing the wrong wall-clock time.
"""

from unittest.mock import patch

import pytest

from app.tasks.generate_embedding import generate_embedding_task


@pytest.mark.unit
def test_local_strftime_accepts_iso_string():
    """The filter must convert UTC ISO strings (the shape produced by
    stages_dict in pipeline_status.py) to the user's local timezone."""
    import app.main  # noqa: F401 — registers the filter on templates.env
    from app.config import templates

    fn = templates.env.filters["local_strftime"]
    # 2026-05-22T22:15:00 UTC → 2026-05-23 00:15 in Europe/Berlin (CEST = UTC+2)
    with patch(
        "app.services.timezone_service.get_user_tz",
        return_value=__import__("zoneinfo").ZoneInfo("Europe/Berlin"),
    ):
        out = fn("2026-05-22T22:15:00", "%Y-%m-%d %H:%M")
    assert out == "2026-05-23 00:15"


@pytest.mark.unit
def test_local_strftime_handles_empty_and_garbage():
    """Empty/None inputs render to empty string. Garbage strings don't crash."""
    import app.main  # noqa: F401
    from app.config import templates

    fn = templates.env.filters["local_strftime"]
    assert fn(None, "%Y-%m-%d") == ""
    assert fn("", "%Y-%m-%d") == ""
    assert fn("not-an-iso-string", "%Y-%m-%d") == ""


# ---------------------------------------------------------------------------
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
