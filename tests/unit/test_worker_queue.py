"""Tests for the processing queue view (/api/v1/worker-queue) and its helpers."""

from datetime import UTC, datetime

import pytest

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
def test_worker_queue_is_empty_when_quiet(app_client):
    """The queue view serves normally with nothing in flight."""
    response = app_client.get("/api/v1/worker-queue")
    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {
        "executing": 0,
        "queued": 0,
        "failed": 0,
        "ai_inflight": 0,
    }
    assert body["executing"] == body["queued"] == body["failed"] == []


@pytest.mark.unit
def test_counts_reflect_queue_items_not_redis(app_client, db_session, sample_case):
    """The counts must come from queue items (stage rows), not Redis
    sentinels or raw pipeline_state document counts.

    Regression chain: the rail badge once called count_inflight() (Redis),
    then counted Documents by pipeline_state (one per doc, even when a doc
    had multiple concurrent stages). Now the counts are derived from the
    same items the view lists."""
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

    response = app_client.get("/api/v1/worker-queue")
    assert response.status_code == 200
    assert response.json()["counts"]["executing"] == 1
    assert response.json()["counts"]["queued"] == 0


@pytest.mark.unit
def test_counts_match_items(app_client, db_session, sample_case):
    """The header counts must equal the listed items at the same DB snapshot.

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

    body = app_client.get("/api/v1/worker-queue").json()
    assert (body["counts"]["executing"], body["counts"]["queued"]) == (1, 1)
    assert [i["label"] for i in body["executing"]] == ["Doc RUNNING"]
    assert [i["label"] for i in body["queued"]] == ["Doc PENDING"]


@pytest.mark.unit
def test_panel_separates_executing_from_queued(db_session, sample_case):
    """The new executing/queued split: a stage in RUNNING shows as executing,
    a stage in PENDING shows as queued. Counts match items, not pipeline_state.

    This is the fix for the screenshot bug: '8 running' was counting
    pipeline_state=PARTIAL docs whose stages were all queued — only one
    doc's stage was actually executing on a worker."""
    from app.services.worker_queue import _build_queue_items

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
    from app.services.worker_queue import _build_queue_items

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
@pytest.mark.parametrize("inflight", [3, 0])
def test_ai_inflight_count_comes_from_the_sentinel(app_client, monkeypatch, inflight):
    """counts.ai_inflight mirrors count_inflight() (the AI-calls chip)."""
    import app.api.v1.worker_queue as wq_route

    monkeypatch.setattr(wq_route, "count_inflight", lambda: inflight)

    response = app_client.get("/api/v1/worker-queue")
    assert response.status_code == 200
    assert response.json()["counts"]["ai_inflight"] == inflight


@pytest.mark.unit
def test_build_queue_items_groups_batch_analysis_docs(db_session, sample_case):
    """Docs sharing BATCH_ANALYSIS + same batch_id collapse into one batch item."""
    from app.services.worker_queue import _build_queue_items

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
    from app.services.worker_queue import _build_queue_items

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
def test_doc_items_carry_batch_and_doc_ids(app_client, db_session, sample_case):
    """Flat doc items expose both ids so the queue modal can badge them
    (B#<batch_id> / D#<doc_id>)."""
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

    response = app_client.get("/api/v1/worker-queue")
    assert response.status_code == 200
    (item,) = response.json()["executing"]
    assert item["kind"] == "doc"
    assert item["doc_id"] == doc.id
    assert item["batch_id"] == batch.id
    assert item["label"] == "Badge Test Doc"


def _scan_batch(db_session, slicing: dict) -> IngestBatch:
    from app.models.database import User

    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.SCAN,
        subject="stack.pdf",
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": slicing},
    )
    db_session.add(batch)
    db_session.commit()
    return batch


@pytest.mark.unit
def test_scans_awaiting_slicing_appear_in_the_queue(app_client, db_session):
    """A scan being prepared executes; one waiting for the user's cuts is queued."""
    preparing = _scan_batch(
        db_session,
        {"status": "preparing", "progress": {"done": 23, "total": 69, "phase": "ocr"}},
    )
    ready = _scan_batch(db_session, {"status": "ready"})

    body = app_client.get("/api/v1/worker-queue").json()

    assert body["counts"]["executing"] == 1
    assert body["counts"]["queued"] == 1
    (running,) = body["executing"]
    assert (running["kind"], running["batch_id"], running["stage"]) == (
        "slicing",
        preparing.id,
        None,
    )
    assert running["note"] == "Preparing (23/69)"
    (waiting,) = body["queued"]
    assert (waiting["kind"], waiting["batch_id"]) == ("slicing", ready.id)
    assert waiting["note"] == "Ready — review cuts"


@pytest.mark.unit
def test_failed_slicing_prep_is_queued_with_a_retry_note(app_client, db_session):
    """A failed preparation waits on the user (retry), so it is queued, not executing."""
    _scan_batch(db_session, {"status": "failed", "error": "boom"})

    body = app_client.get("/api/v1/worker-queue").json()

    assert body["executing"] == []
    (item,) = body["queued"]
    assert item["note"] == "Preparation failed — open to retry"
    assert item["label"] == "Scan #" + str(item["batch_id"]) + " — stack.pdf"
