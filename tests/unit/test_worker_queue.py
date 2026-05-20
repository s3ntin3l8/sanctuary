"""Tests for the Processing Queue endpoints and helper functions.

The global `PRAGMA busy_timeout = 60000` (app/config.py) lets a contended
write path wait up to 60s for the writer lock. That's appropriate for worker
writes, but the /badge and /panel reads have a sub-second latency budget —
without an override, they hang for up to a minute when the cascade is busy,
which surfaces as "Loading…" forever in the UI. `_fail_fast_reads` overrides
the per-connection busy_timeout to 1000ms so a contended read raises within
~1s and the UI swaps the placeholder for an error state.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.models.database import Document, DocumentPipelineStage, IngestBatch
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    PipelineStage,
    PipelineState,
    StageStatus,
)


@pytest.mark.unit
def test_fail_fast_reads_sets_one_second_busy_timeout(db_session):
    """_fail_fast_reads must override the connection's busy_timeout to ~1s.

    Reads the PRAGMA back on the same connection to prove the override took
    effect — relying on indirect proxies (e.g. timed contention) would make
    the test flaky on shared CI.
    """
    from app.api.worker_queue import _READ_BUSY_TIMEOUT_MS, _fail_fast_reads

    _fail_fast_reads(db_session)

    current = db_session.execute(text("PRAGMA busy_timeout")).scalar()
    assert current == _READ_BUSY_TIMEOUT_MS
    assert _READ_BUSY_TIMEOUT_MS == 1000  # contract: documented in the docstring


@pytest.mark.unit
def test_worker_queue_badge_endpoint_returns_200_when_quiet(app_client):
    """End-to-end: the /badge endpoint must serve normally when there's no
    contention. Regression guard against the PRAGMA override breaking the
    happy path."""
    response = app_client.get("/api/worker/queue/badge")
    assert response.status_code == 200


@pytest.mark.unit
def test_worker_queue_panel_endpoint_returns_200_when_quiet(app_client):
    """End-to-end: the /panel endpoint must serve normally when there's no
    contention."""
    response = app_client.get("/api/worker/queue/panel")
    assert response.status_code == 200


@pytest.mark.unit
def test_build_queue_items_groups_batch_analysis_docs(db_session, sample_case):
    """Docs sharing BATCH_ANALYSIS + same batch_id collapse into one batch item."""
    from app.api.worker_queue import _build_queue_items

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        received_at=datetime.now(UTC),
        case_id=sample_case.id,
        status=IngestBatchStatus.PROCESSING,
        subject="Test Email Subject",
    )
    db_session.add(batch)
    db_session.flush()

    docs = []
    for i in range(3):
        doc = Document(
            title=f"Doc {i}",
            content="content",
            case_id=sample_case.id,
            originator_type=OriginatorType.COURT,
            ingest_batch_id=batch.id,
            pipeline_state=PipelineState.RUNNING,
        )
        db_session.add(doc)
        db_session.flush()
        db_session.add(
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.BATCH_ANALYSIS.value,
                status=StageStatus.RUNNING.value,
            )
        )
        docs.append(doc)
    db_session.commit()

    for doc in docs:
        db_session.refresh(doc)

    items = _build_queue_items(docs, [])

    assert len(items) == 1
    item = items[0]
    assert item["type"] == "batch"
    assert len(item["docs"]) == 3
    assert item["stage"] == PipelineStage.BATCH_ANALYSIS


@pytest.mark.unit
def test_build_queue_items_non_batch_stage_stays_flat(db_session, sample_case):
    """Docs in a per-doc stage (enrich) remain individual items even if same batch."""
    from app.api.worker_queue import _build_queue_items

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        received_at=datetime.now(UTC),
        case_id=sample_case.id,
        status=IngestBatchStatus.PROCESSING,
    )
    db_session.add(batch)
    db_session.flush()

    docs = []
    for i in range(2):
        doc = Document(
            title=f"Doc {i}",
            content="content",
            case_id=sample_case.id,
            originator_type=OriginatorType.COURT,
            ingest_batch_id=batch.id,
            pipeline_state=PipelineState.RUNNING,
        )
        db_session.add(doc)
        db_session.flush()
        db_session.add(
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.ENRICH.value,
                status=StageStatus.RUNNING.value,
            )
        )
        docs.append(doc)
    db_session.commit()

    for doc in docs:
        db_session.refresh(doc)

    items = _build_queue_items(docs, [])

    assert len(items) == 2
    assert all(item["type"] == "doc" for item in items)
