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
def test_badge_reflects_queue_items_not_redis(app_client, db_session, sample_case):
    """Badge endpoint must count queue items (same as the panel header),
    not Redis sentinels or raw pipeline_state document counts.

    Regression chain: badge once called count_inflight() (Redis), then
    counted Documents by pipeline_state (one per doc, even when a doc had
    multiple concurrent stages). Now it shares compute_queue_counts() with
    the panel, so badge == "X Active" always."""
    from app.models.enums import OriginatorType

    doc = Document(
        title="Test doc",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
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
    db_session.commit()

    response = app_client.get("/api/worker/queue/badge")
    assert response.status_code == 200
    assert b"1" in response.content


@pytest.mark.unit
def test_badge_and_panel_header_agree(app_client, db_session, sample_case):
    """Badge count must equal the panel header 'X Active' at the same DB snapshot.

    Realistic fixture: production docs always have stage rows (initialize()
    is called at ingest time). The panel's queue items come from those stage
    rows — a doc without stage rows produces no items and would render as
    idle, even though pipeline_state says otherwise. Tests must mirror that
    invariant or they're testing a state that can't exist."""
    from app.models.enums import OriginatorType

    # Doc 1: pipeline_state=RUNNING with a stage in RUNNING → one executing item.
    doc_running = Document(
        title="Doc RUNNING",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
        pipeline_state=PipelineState.RUNNING,
    )
    db_session.add(doc_running)
    db_session.flush()
    db_session.add(
        DocumentPipelineStage(
            document_id=doc_running.id,
            stage=PipelineStage.ENRICH.value,
            status=StageStatus.RUNNING.value,
        )
    )

    # Doc 2: pipeline_state=PENDING with a pending stage → one queued item.
    doc_pending = Document(
        title="Doc PENDING",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
        pipeline_state=PipelineState.PENDING,
    )
    db_session.add(doc_pending)
    db_session.flush()
    db_session.add(
        DocumentPipelineStage(
            document_id=doc_pending.id,
            stage=PipelineStage.EXTRACT.value,
            status=StageStatus.PENDING.value,
        )
    )
    db_session.commit()

    badge_resp = app_client.get("/api/worker/queue/badge")
    panel_resp = app_client.get("/api/worker/queue/panel")
    assert badge_resp.status_code == 200
    assert panel_resp.status_code == 200
    # Both should show 2 (1 executing + 1 queued)
    assert b"2" in badge_resp.content
    assert b"2 Active" in panel_resp.content


@pytest.mark.unit
def test_panel_separates_executing_from_queued(db_session, sample_case):
    """The new executing/queued split: a stage in RUNNING shows as executing,
    a stage in PENDING shows as queued. Counts match items, not pipeline_state.

    This is the fix for the screenshot bug: '8 running' was counting
    pipeline_state=PARTIAL docs whose stages were all queued — only one
    doc's stage was actually executing on a worker."""
    from app.api.worker_queue import _build_queue_items

    # One doc with a RUNNING stage — should be executing.
    doc_executing = Document(
        title="Executing doc",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
        pipeline_state=PipelineState.PARTIAL,
    )
    db_session.add(doc_executing)
    db_session.flush()
    db_session.add(
        DocumentPipelineStage(
            document_id=doc_executing.id,
            stage=PipelineStage.METADATA.value,
            status=StageStatus.RUNNING.value,
        )
    )

    # Several docs with all stages PENDING — should be queued.
    queued_docs = []
    for i in range(3):
        d = Document(
            title=f"Queued doc {i}",
            content="x",
            case_id=sample_case.id,
            originator_type=OriginatorType.COURT,
            pipeline_state=PipelineState.PARTIAL,
        )
        db_session.add(d)
        db_session.flush()
        db_session.add(
            DocumentPipelineStage(
                document_id=d.id,
                stage=PipelineStage.EXTRACT.value,
                status=StageStatus.PENDING.value,
            )
        )
        queued_docs.append(d)
    db_session.commit()
    db_session.refresh(doc_executing)
    for d in queued_docs:
        db_session.refresh(d)

    items = _build_queue_items([doc_executing] + queued_docs, [])
    n_executing = sum(1 for it in items if it.get("executing"))
    n_queued = sum(1 for it in items if not it.get("executing"))
    assert n_executing == 1, f"expected 1 executing item, got {n_executing}"
    assert n_queued == 3, f"expected 3 queued items, got {n_queued}"


@pytest.mark.unit
def test_panel_retrying_classified_as_queued(db_session, sample_case):
    """A stage in RETRYING (waiting for the retry countdown) is NOT a worker
    actively processing — it's queued. Goes in the queued section."""
    from app.api.worker_queue import _build_queue_items

    doc = Document(
        title="Retrying doc",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
        pipeline_state=PipelineState.PARTIAL,
    )
    db_session.add(doc)
    db_session.flush()
    db_session.add(
        DocumentPipelineStage(
            document_id=doc.id,
            stage=PipelineStage.ENRICH.value,
            status=StageStatus.RETRYING.value,
        )
    )
    db_session.commit()
    db_session.refresh(doc)

    items = _build_queue_items([doc], [])
    assert len(items) == 1
    assert items[0]["executing"] is False


@pytest.mark.unit
def test_ai_calls_chip_appears_when_inflight(app_client, monkeypatch):
    """The 'X AI calls' chip appears in the panel body when count_inflight() > 0."""
    import app.api.worker_queue as wq_module

    monkeypatch.setattr(wq_module, "count_inflight", lambda: 3)

    response = app_client.get("/api/worker/queue/panel")
    assert response.status_code == 200
    assert b"3" in response.content
    assert b"AI calls" in response.content


@pytest.mark.unit
def test_ai_calls_chip_absent_when_idle(app_client, monkeypatch):
    """The 'AI calls' chip must not appear when count_inflight() returns 0."""
    import app.api.worker_queue as wq_module

    monkeypatch.setattr(wq_module, "count_inflight", lambda: 0)

    response = app_client.get("/api/worker/queue/panel")
    assert response.status_code == 200
    assert b"AI calls" not in response.content


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


@pytest.mark.unit
def test_panel_doc_rows_show_batch_and_doc_id_badges(
    app_client, db_session, sample_case
):
    """Doc rows must render B#<batch_id> and D#<doc_id> badges inline before the title.

    Covers: standalone doc rows (executing/queued) and failed doc rows.
    Batch member rows only show D# — tested via the batch grouping fixture
    which already asserts item structure; the badge presence here covers the
    flat-doc path that is most common.
    """
    from app.models.enums import OriginatorType

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        received_at=datetime.now(UTC),
        case_id=sample_case.id,
        status=IngestBatchStatus.PROCESSING,
        subject="Badge Test Email",
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="Badge Test Doc",
        content="x",
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
    db_session.commit()
    db_session.refresh(doc)

    response = app_client.get("/api/worker/queue/panel")
    assert response.status_code == 200
    html = response.text

    batch_badge = f"B#{batch.id}"
    doc_badge = f"D#{doc.id}"
    assert batch_badge in html, f"Expected '{batch_badge}' in panel HTML"
    assert doc_badge in html, f"Expected '{doc_badge}' in panel HTML"
