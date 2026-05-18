"""Tests for pipeline stage idempotency: re-running a completed stage is safe."""

import pytest

from app.models.enums import PipelineStage, StageStatus


@pytest.mark.unit
def test_extract_already_completed_is_skipped_by_task(db_session, monkeypatch):
    """process_document_task skips the EXTRACT block when the stage is already completed."""
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

    # Mark EXTRACT completed up front
    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    mark_completed(doc.id, PipelineStage.EXTRACT, db_session)

    # Stub out downstream calls so we don't need AI or real files
    monkeypatch.setattr(
        "app.tasks.document_processing._run_phase1_summary", lambda doc_id: None
    )
    monkeypatch.setattr(
        "app.tasks.generate_embedding.generate_embedding_task.delay",
        lambda doc_id: None,
    )

    called = []
    monkeypatch.setattr(
        "app.services.ingestion.service.process_uploaded_document",
        lambda doc, db: called.append("called"),
    )
    monkeypatch.setattr(
        "app.dependencies.get_db_session",
        lambda: db_session,
    )

    from app.tasks.document_processing import process_document_task

    process_document_task(doc.id)

    assert not called, (
        "process_uploaded_document was called even though EXTRACT was already completed"
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
    status = doc.pipeline_stages[PipelineStage.EMBEDDINGS.value]["status"]
    assert status == StageStatus.COMPLETED.value
