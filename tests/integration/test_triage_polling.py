"""Verify that the pipeline aggregate polling endpoint avoids full-feed rebuilds."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document, IngestBatch
from app.models.enums import (
    CaseStatus,
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
    StageStatus,
)
from app.services.pipeline_status import initialize

client = TestClient(app)


@pytest.mark.integration
def test_bundle_pipeline_endpoint_returns_404_for_unknown_batch():
    resp = client.get("/triage/bundle/999999/pipeline")
    assert resp.status_code == 404


@pytest.mark.integration
def test_bundle_pipeline_endpoint_returns_200_for_existing_batch(db_session):
    """GET /triage/bundle/{id}/pipeline returns 200 and a pipeline chip."""
    from app.models.database import Case

    case = Case(
        id="_TP1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
        subject="test batch",
        received_at=datetime.now(UTC),
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)

    doc = Document(
        title="doc",
        content="content",
        case_id="_TP1",
        originator_type=OriginatorType.COURT,
        ingest_batch_id=batch.id,
        role=DocumentRole.ENCLOSURE,
        ingest_date=datetime.now(UTC),
    )
    initialize(doc, batched=True)
    db_session.add(doc)
    db_session.commit()

    resp = client.get(f"/triage/bundle/{batch.id}/pipeline")
    assert resp.status_code == 200
    # Template renders a span with the bundle's batch ID for polling self-targeting
    assert f"pipeline-agg-batch-{batch.id}" in resp.text


@pytest.mark.integration
def test_bundle_pipeline_endpoint_shows_running_chip(db_session):
    """Running stages render the 'processing' chip label."""
    from app.models.database import Case
    from app.models.enums import PipelineState

    case = Case(
        id="_TP2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
        subject="test batch 2",
        received_at=datetime.now(UTC),
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)

    running_stages = {
        s.value: {"status": StageStatus.PENDING.value} for s in PipelineStage
    }
    running_stages[PipelineStage.EXTRACT.value]["status"] = StageStatus.RUNNING.value

    doc = Document(
        title="doc",
        content="content",
        case_id="_TP2",
        originator_type=OriginatorType.COURT,
        ingest_batch_id=batch.id,
        role=DocumentRole.ENCLOSURE,
        ingest_date=datetime.now(UTC),
        pipeline_stages=running_stages,
        pipeline_state=PipelineState.RUNNING,
    )
    db_session.add(doc)
    db_session.commit()

    resp = client.get(f"/triage/bundle/{batch.id}/pipeline")
    assert resp.status_code == 200
    assert "processing" in resp.text


@pytest.mark.integration
def test_bundle_pipeline_endpoint_triggers_reload_when_all_done(db_session):
    """HX-Trigger reload header is emitted when all stages are terminal."""
    from app.models.database import Case
    from app.models.enums import PipelineState

    case = Case(
        id="_TP3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
        subject="test batch 3",
        received_at=datetime.now(UTC),
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)

    done_stages = {
        s.value: {"status": StageStatus.COMPLETED.value} for s in PipelineStage
    }

    doc = Document(
        title="doc",
        content="content",
        case_id="_TP3",
        originator_type=OriginatorType.COURT,
        ingest_batch_id=batch.id,
        role=DocumentRole.ENCLOSURE,
        ingest_date=datetime.now(UTC),
        pipeline_stages=done_stages,
        pipeline_state=PipelineState.COMPLETED,
    )
    db_session.add(doc)
    db_session.commit()

    resp = client.get(f"/triage/bundle/{batch.id}/pipeline")
    assert resp.status_code == 200
    assert "HX-Trigger" in resp.headers
    assert f"reload-bundle-{batch.id}" in resp.headers["HX-Trigger"]
