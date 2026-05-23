"""Tests for pipeline stage idempotency: re-running a completed stage is safe."""

import pytest

from app.models.enums import PipelineStage, StageStatus
from app.services.pipeline_status import stages_dict


@pytest.mark.unit
def test_metadata_already_completed_is_skipped_by_task(db_session, monkeypatch):
    """metadata_task skips _run_phase1_summary when METADATA is already completed.

    Recovery may re-dispatch metadata_task for a doc whose METADATA finished
    in a prior run; re-running the LLM call would be wasteful.
    """
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_completed, mark_started

    case = Case(
        id="_IP1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_IP1",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()
    db_session.refresh(doc)

    # Mark METADATA completed up front
    mark_started(doc.id, PipelineStage.METADATA, db_session)
    mark_completed(doc.id, PipelineStage.METADATA, db_session)

    called = []
    monkeypatch.setattr(
        "app.tasks.document_processing._run_phase1_summary",
        lambda doc_id: called.append("called"),
    )
    monkeypatch.setattr(
        "app.tasks.generate_embedding.generate_embedding_task.delay",
        lambda doc_id: None,
    )
    monkeypatch.setattr(
        "app.dependencies.get_db_session",
        lambda: db_session,
    )

    from app.tasks.document_processing import metadata_task

    metadata_task(doc.id)

    assert not called, (
        "_run_phase1_summary was called even though METADATA was already completed"
    )


@pytest.mark.unit
def test_mark_started_then_completed_idempotent_state(db_session):
    """Calling mark_started + mark_completed twice on the same stage yields COMPLETED, not RUNNING."""
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_completed, mark_started

    case = Case(
        id="_IP2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_IP2",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()
    db_session.refresh(doc)

    mark_started(doc.id, PipelineStage.EMBEDDINGS, db_session)
    mark_completed(doc.id, PipelineStage.EMBEDDINGS, db_session)
    # Second pass (simulating a re-run after crash recovery)
    mark_started(doc.id, PipelineStage.EMBEDDINGS, db_session)
    mark_completed(doc.id, PipelineStage.EMBEDDINGS, db_session)

    db_session.refresh(doc)
    status = stages_dict(doc)[PipelineStage.EMBEDDINGS.value]["status"]
    assert status == StageStatus.COMPLETED.value


@pytest.mark.unit
def test_metadata_task_skips_when_stage_already_claimed(db_session, monkeypatch):
    """Concurrent metadata_task dispatches: only one runs _run_phase1_summary.

    Race window: process_document_task fires metadata_task on EXTRACT success,
    and recover_stuck_pending_dispatches can fire a second copy if METADATA
    has been pending for >60s. Both runners would otherwise call the LLM and
    burn a redundant Phase 1 round-trip.

    Simulated here by pre-marking METADATA as RUNNING (the state a concurrent
    winner would have left it in) before invoking metadata_task.
    """
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_completed, mark_started

    case = Case(
        id="_IP3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_IP3",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()
    db_session.refresh(doc)

    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    mark_completed(doc.id, PipelineStage.EXTRACT, db_session)
    # Concurrent winner already flipped METADATA → RUNNING via
    # claim_stage_for_dispatch. The second runner's claim must fail.
    mark_started(doc.id, PipelineStage.METADATA, db_session)

    called = []
    monkeypatch.setattr(
        "app.tasks.document_processing._run_phase1_summary",
        lambda doc_id: called.append("called"),
    )
    monkeypatch.setattr(
        "app.dependencies.get_db_session",
        lambda: db_session,
    )

    from app.tasks.document_processing import metadata_task

    result = metadata_task(doc.id)

    assert not called, (
        "_run_phase1_summary was called even though METADATA was already RUNNING"
    )
    assert result["status"] == "already_claimed"
