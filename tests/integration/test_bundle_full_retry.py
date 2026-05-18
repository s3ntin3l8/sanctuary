"""Integration tests for POST /triage/bundle/retry with full=true."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.models.database import (
    Case,
    Document,
    DocumentPipelineStage,
    IngestBatch,
    Proceeding,
)
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    PipelineStage,
    PipelineState,
    StageStatus,
)


def _make_batch(db_session, case_id=None) -> IngestBatch:
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        received_at=datetime.now(UTC),
        case_id=case_id,
        status=IngestBatchStatus.PENDING,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _make_doc(
    db_session, batch: IngestBatch, case_id=None, proceeding_id=None
) -> Document:
    doc = Document(
        title="Test Doc",
        content="Some content",
        case_id=case_id or batch.case_id,
        proceeding_id=proceeding_id,
        originator_type=OriginatorType.COURT,
        ingest_batch_id=batch.id,
        pipeline_state=PipelineState.COMPLETED,
    )
    db_session.add(doc)
    db_session.flush()
    for s in PipelineStage:
        db_session.add(
            DocumentPipelineStage(
                document_id=doc.id,
                stage=s.value,
                status=StageStatus.COMPLETED.value,
            )
        )
    db_session.flush()
    return doc


def _make_case(db_session, case_id, is_draft=False) -> Case:
    c = Case(id=case_id, title=f"Case {case_id}", is_draft=is_draft)
    db_session.add(c)
    db_session.flush()
    return c


def _make_proceeding(db_session, case_id, is_draft=False) -> Proceeding:
    from app.models.enums import ProceedingCourtLevel

    p = Proceeding(
        case_id=case_id,
        court_name="Test Court",
        court_level=ProceedingCourtLevel.AG,
        is_draft=is_draft,
    )
    db_session.add(p)
    db_session.flush()
    return p


@pytest.mark.integration
def test_full_retry_resets_extract_and_dispatches_it(
    app_client, db_session, sample_case
):
    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc(db_session, batch)
    db_session.commit()

    with patch("app.services.triage_retry.dispatch_pipeline_retry") as mock_dispatch:
        response = app_client.post(
            "/triage/bundle/retry", data={"batch_id": batch.id, "full": "true"}
        )

    assert response.status_code == 200
    db_session.refresh(doc)

    # EXTRACT must be PENDING
    assert (
        doc.pipeline_stages[PipelineStage.EXTRACT.value]["status"]
        == StageStatus.PENDING.value
    )

    # Only EXTRACT (head) + EMBEDDINGS (parallel) should be dispatched
    dispatched_stages = {call.args[2] for call in mock_dispatch.call_args_list}
    assert PipelineStage.EXTRACT in dispatched_stages
    assert PipelineStage.EMBEDDINGS in dispatched_stages


@pytest.mark.integration
def test_full_retry_preserves_confirmed_case(app_client, db_session):
    c = _make_case(db_session, "CONF-001", is_draft=False)
    batch = _make_batch(db_session, c.id)
    doc = _make_doc(db_session, batch)
    db_session.commit()

    with patch("app.services.triage_retry.dispatch_pipeline_retry"):
        app_client.post(
            "/triage/bundle/retry", data={"batch_id": batch.id, "full": "true"}
        )

    db_session.refresh(doc)
    assert doc.case_id == "CONF-001"


@pytest.mark.integration
def test_full_retry_resets_draft_case(app_client, db_session):
    c = _make_case(db_session, "DRAFT-001", is_draft=True)
    batch = _make_batch(db_session, c.id)
    doc = _make_doc(db_session, batch)
    db_session.commit()

    with patch("app.services.triage_retry.dispatch_pipeline_retry"):
        app_client.post(
            "/triage/bundle/retry", data={"batch_id": batch.id, "full": "true"}
        )

    db_session.refresh(doc)
    assert doc.case_id == "_TRIAGE"


@pytest.mark.integration
def test_full_retry_preserves_confirmed_proceeding(app_client, db_session):
    c = _make_case(db_session, "CONF-002", is_draft=False)
    p = _make_proceeding(db_session, c.id, is_draft=False)
    batch = _make_batch(db_session, c.id)
    doc = _make_doc(db_session, batch, case_id=c.id, proceeding_id=p.id)
    db_session.commit()

    with patch("app.services.triage_retry.dispatch_pipeline_retry"):
        app_client.post(
            "/triage/bundle/retry", data={"batch_id": batch.id, "full": "true"}
        )

    db_session.refresh(doc)
    assert doc.proceeding_id == p.id


@pytest.mark.integration
def test_full_retry_resets_draft_proceeding(app_client, db_session):
    c = _make_case(db_session, "CONF-003", is_draft=False)
    p = _make_proceeding(db_session, c.id, is_draft=True)
    batch = _make_batch(db_session, c.id)
    doc = _make_doc(db_session, batch, case_id=c.id, proceeding_id=p.id)
    db_session.commit()

    with patch("app.services.triage_retry.dispatch_pipeline_retry"):
        app_client.post(
            "/triage/bundle/retry", data={"batch_id": batch.id, "full": "true"}
        )

    db_session.refresh(doc)
    assert doc.proceeding_id is None


@pytest.mark.integration
def test_standard_retry_still_skips_extract(app_client, db_session, sample_case):
    batch = _make_batch(db_session, sample_case.id)
    doc = _make_doc(db_session, batch)
    db_session.commit()

    with patch("app.services.triage_retry.dispatch_pipeline_retry") as mock_dispatch:
        # full=false is default
        app_client.post("/triage/bundle/retry", data={"batch_id": batch.id})

    db_session.refresh(doc)
    # EXTRACT stays COMPLETED
    assert (
        doc.pipeline_stages[PipelineStage.EXTRACT.value]["status"]
        == StageStatus.COMPLETED.value
    )

    dispatched_stages = {call.args[2] for call in mock_dispatch.call_args_list}
    assert PipelineStage.EXTRACT not in dispatched_stages
