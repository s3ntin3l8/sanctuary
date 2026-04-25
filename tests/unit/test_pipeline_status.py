"""Tests for the pipeline_status stage registry and atomic-commit semantics."""

import pytest

from app.models.enums import PipelineStage, PipelineState, StageStatus

# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_registry_covers_every_stage():
    """Every PipelineStage member must have a STAGE_REGISTRY entry.

    The module-level guard in pipeline_status already raises on import
    if an entry is missing; this test makes the contract explicit and
    gives a clear failure message when a new enum member is added.
    """
    from app.services.pipeline_status import STAGE_REGISTRY

    assert set(STAGE_REGISTRY.keys()) == set(PipelineStage)


@pytest.mark.unit
def test_stage_order_is_unique():
    """No two stages share the same order value."""
    from app.services.pipeline_status import STAGE_REGISTRY

    orders = [spec.order for spec in STAGE_REGISTRY.values()]
    assert len(orders) == len(set(orders))


@pytest.mark.unit
def test_retry_tasks_are_non_empty():
    """Every stage spec has a non-empty retry_task dotted path."""
    from app.services.pipeline_status import STAGE_REGISTRY

    for stage, spec in STAGE_REGISTRY.items():
        assert spec.retry_task, f"STAGE_REGISTRY[{stage}].retry_task is empty"


# ---------------------------------------------------------------------------
# PROCEEDING_ANALYSIS regression (finding #1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_upstream_blocking_works_for_proceeding_analysis(db_session):
    """get_upstream_blocking must not raise ValueError for PROCEEDING_ANALYSIS.

    Prior to the registry, this raised because PROCEEDING_ANALYSIS was absent
    from _STAGE_ORDER and .index() threw.
    """
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import get_upstream_blocking, initialize

    case = Case(
        id="_T1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_T1",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    stages = doc.pipeline_stages or {}
    # Should return a list, not raise ValueError
    result = get_upstream_blocking(PipelineStage.PROCEEDING_ANALYSIS, stages)
    assert isinstance(result, list)


@pytest.mark.unit
def test_reset_stage_cascades_to_proceeding_analysis(db_session):
    """reset_stage(EXTRACT) must reset PROCEEDING_ANALYSIS to PENDING."""
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, reset_stage

    case = Case(
        id="_T2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_T2",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=True)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    # Mark proceeding_analysis completed so it's not PENDING to start
    from app.services.pipeline_status import mark_completed, mark_started

    mark_started(doc.id, PipelineStage.PROCEEDING_ANALYSIS, db_session)
    mark_completed(doc.id, PipelineStage.PROCEEDING_ANALYSIS, db_session)

    db_session.refresh(doc)
    assert (
        doc.pipeline_stages[PipelineStage.PROCEEDING_ANALYSIS.value]["status"]
        == StageStatus.COMPLETED.value
    )

    # Now reset from EXTRACT — cascades should hit PROCEEDING_ANALYSIS
    reset_stage(doc.id, PipelineStage.EXTRACT, db_session)

    db_session.refresh(doc)
    pa_status = doc.pipeline_stages[PipelineStage.PROCEEDING_ANALYSIS.value]["status"]
    assert pa_status == StageStatus.PENDING.value, (
        f"Expected PROCEEDING_ANALYSIS to be PENDING after resetting EXTRACT, got {pa_status}"
    )


# ---------------------------------------------------------------------------
# Atomic commit semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mark_completed_respects_commit_false(db_session):
    """mark_completed(commit=False) stages the change but does not flush to DB.

    Rolling back the session after calling mark_completed(commit=False) leaves
    the pipeline_stages row unchanged — the stage is still RUNNING, not COMPLETED.
    """
    from sqlalchemy import text

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_completed, mark_started

    case = Case(
        id="_T3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_T3",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    mark_started(doc.id, PipelineStage.EMBEDDINGS, db_session)

    # Call mark_completed without committing
    mark_completed(doc.id, PipelineStage.EMBEDDINGS, db_session, commit=False)

    # Rolling back undoes the pending stage update
    db_session.rollback()

    # The row in the DB should still show RUNNING (from mark_started which committed)
    row = db_session.execute(
        text("SELECT pipeline_stages FROM documents WHERE id = :id"),
        {"id": doc.id},
    ).fetchone()
    import json

    stages = json.loads(row[0])
    assert stages[PipelineStage.EMBEDDINGS.value]["status"] == StageStatus.RUNNING.value


@pytest.mark.unit
def test_sequential_stage_updates_both_survive(db_session):
    """Updating two different stages sequentially leaves both updates persisted.

    Verifies that the read-modify-write in _update_stage does not lose earlier
    stage data when a later stage is written: each call patches exactly its own
    key inside pipeline_stages without clobbering adjacent keys.
    """
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_completed, mark_started

    case = Case(
        id="_T4", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_T4",
        originator_type=OriginatorType.COURT,
    )
    initialize(doc, batched=False)
    db_session.add(doc)
    db_session.commit()

    mark_started(doc.id, PipelineStage.CLAIMS, db_session)
    mark_completed(doc.id, PipelineStage.CLAIMS, db_session)
    mark_started(doc.id, PipelineStage.ENTITIES, db_session)
    mark_completed(doc.id, PipelineStage.ENTITIES, db_session)

    db_session.refresh(doc)
    stages = doc.pipeline_stages or {}
    assert (
        stages.get(PipelineStage.CLAIMS.value, {}).get("status")
        == StageStatus.COMPLETED.value
    )
    assert (
        stages.get(PipelineStage.ENTITIES.value, {}).get("status")
        == StageStatus.COMPLETED.value
    )


# ---------------------------------------------------------------------------
# aggregate_pipeline_summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_pipeline_summary_counts_by_state():
    """aggregate_pipeline_summary returns correct per-state counts."""
    from app.services.pipeline_status import aggregate_pipeline_summary

    all_stages = {
        s.value: {"status": StageStatus.COMPLETED.value} for s in PipelineStage
    }
    result = aggregate_pipeline_summary([all_stages, all_stages])
    assert result["total"] == 2
    assert result.get(PipelineState.COMPLETED.value, 0) == 2


@pytest.mark.unit
def test_aggregate_pipeline_summary_empty():
    from app.services.pipeline_status import aggregate_pipeline_summary

    result = aggregate_pipeline_summary([])
    assert result["total"] == 0
