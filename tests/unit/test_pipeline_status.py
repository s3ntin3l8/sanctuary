"""Tests for the pipeline_status stage registry and atomic-commit semantics."""

import pytest

from app.models.enums import PipelineStage, PipelineState, StageStatus
from app.services.pipeline_status import stages_dict

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
def test_stage_display_list_reflects_wallclock_order():
    """The pipeline stepper renders this list left-to-right. EMBEDDINGS
    dispatches alongside BATCH_ANALYSIS but finishes much earlier (it's
    parallel from METADATA-done), so it sits at index 2 — right after
    Metadata, before Batch Analysis. ENTITIES is the rightmost stage on
    the brief-relevant fast-path."""
    from app.services.pipeline_status import stage_display_list

    rows = stage_display_list()
    keys = [r["key"] for r in rows]
    assert keys == [
        "extract",
        "metadata",
        "embeddings",
        "batch_analysis",
        "enrich",
        "relationships",
        "claims",
        "entities",
    ]
    # Each row carries the display fields the stepper template consumes.
    for r in rows:
        assert {"key", "icon", "label"} <= r.keys()
        assert r["icon"] and r["label"]


@pytest.mark.unit
def test_stage_specs_have_label_and_icon():
    """Every registered stage must carry the label + icon fields so the
    stepper template can render it without falling back to bare keys."""
    from app.services.pipeline_status import STAGE_REGISTRY

    for stage, spec in STAGE_REGISTRY.items():
        assert spec.label, f"STAGE_REGISTRY[{stage}].label is empty"
        assert spec.icon, f"STAGE_REGISTRY[{stage}].icon is empty"


@pytest.mark.unit
def test_depends_on_forms_acyclic_dag():
    """Every transitive upstream set must be acyclic and reachable via depends_on."""
    from app.services.pipeline_status import _UPSTREAM, STAGE_REGISTRY

    # No stage is its own ancestor
    for stage, ancestors in _UPSTREAM.items():
        assert stage not in ancestors, f"{stage} appears in its own upstream — cycle"

    # Every depends_on edge points to a registered stage
    for stage, spec in STAGE_REGISTRY.items():
        for dep in spec.depends_on:
            assert dep in STAGE_REGISTRY, f"{stage}.depends_on contains unknown {dep}"


@pytest.mark.unit
def test_upstream_blocking_uses_dependency_graph_not_order():
    """Parallel sibling stages with lower display order are NOT considered
    upstream — this is the regression EMBEDDINGS' order=2 would have caused
    with the old `order < spec.order` logic."""
    from app.models.enums import PipelineStage, StageStatus
    from app.services.pipeline_status import get_upstream_blocking

    # EMBEDDINGS sits at order=2 (between METADATA=1 and BATCH_ANALYSIS=3)
    # but its only real upstream is EXTRACT + METADATA. BATCH_ANALYSIS is a
    # PARALLEL sibling, not an upstream — even though its order is higher,
    # we're testing that the predicate looks at depends_on, not order.
    stages = {
        PipelineStage.EXTRACT.value: {"status": StageStatus.COMPLETED.value},
        PipelineStage.METADATA.value: {"status": StageStatus.COMPLETED.value},
        PipelineStage.BATCH_ANALYSIS.value: {"status": StageStatus.RUNNING.value},
    }
    assert get_upstream_blocking(PipelineStage.EMBEDDINGS, stages) == []


@pytest.mark.unit
def test_upstream_blocking_flags_actual_dependency_running():
    """A real upstream stage in RUNNING is flagged."""
    from app.models.enums import PipelineStage, StageStatus
    from app.services.pipeline_status import get_upstream_blocking

    stages = {
        PipelineStage.EXTRACT.value: {"status": StageStatus.COMPLETED.value},
        PipelineStage.METADATA.value: {"status": StageStatus.RUNNING.value},
    }
    blocking = get_upstream_blocking(PipelineStage.EMBEDDINGS, stages)
    assert blocking == [PipelineStage.METADATA.value]


@pytest.mark.unit
def test_upstream_blocking_for_claims_excludes_parallel_entities():
    """ENTITIES and CLAIMS are parallel sibling outputs of ENRICH. Neither
    should be upstream of the other — even though ENTITIES has order=7 and
    CLAIMS has order=6 (so CLAIMS would have been ENTITIES' upstream under
    the old order-based logic)."""
    from app.models.enums import PipelineStage, StageStatus
    from app.services.pipeline_status import get_upstream_blocking

    stages = {
        PipelineStage.EXTRACT.value: {"status": StageStatus.COMPLETED.value},
        PipelineStage.METADATA.value: {"status": StageStatus.COMPLETED.value},
        PipelineStage.BATCH_ANALYSIS.value: {"status": StageStatus.COMPLETED.value},
        PipelineStage.ENRICH.value: {"status": StageStatus.COMPLETED.value},
        PipelineStage.CLAIMS.value: {"status": StageStatus.RUNNING.value},
    }
    # CLAIMS running must NOT block an ENTITIES retry
    assert get_upstream_blocking(PipelineStage.ENTITIES, stages) == []


@pytest.mark.unit
def test_retry_tasks_are_non_empty():
    """Every stage spec has a non-empty retry_task dotted path."""
    from app.services.pipeline_status import STAGE_REGISTRY

    for stage, spec in STAGE_REGISTRY.items():
        assert spec.retry_task, f"STAGE_REGISTRY[{stage}].retry_task is empty"


# ---------------------------------------------------------------------------
# Atomic commit semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mark_completed_respects_commit_false(db_session):
    """mark_completed(commit=False) stages the change but does not flush to DB.

    Rolling back the session after calling mark_completed(commit=False) leaves
    the document_pipeline_stages row unchanged — the stage is still RUNNING.
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
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()
    db_session.refresh(doc)

    mark_started(doc.id, PipelineStage.EMBEDDINGS, db_session)

    mark_completed(doc.id, PipelineStage.EMBEDDINGS, db_session, commit=False)

    db_session.rollback()

    row = db_session.execute(
        text(
            "SELECT status FROM document_pipeline_stages "
            "WHERE document_id = :id AND stage = :stage"
        ),
        {"id": doc.id, "stage": PipelineStage.EMBEDDINGS.value},
    ).fetchone()
    assert row[0] == StageStatus.RUNNING.value


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
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()

    mark_started(doc.id, PipelineStage.CLAIMS, db_session)
    mark_completed(doc.id, PipelineStage.CLAIMS, db_session)
    mark_started(doc.id, PipelineStage.ENTITIES, db_session)
    mark_completed(doc.id, PipelineStage.ENTITIES, db_session)

    db_session.refresh(doc)
    stages = stages_dict(doc)
    assert (
        stages.get(PipelineStage.CLAIMS.value, {}).get("status")
        == StageStatus.COMPLETED.value
    )
    assert (
        stages.get(PipelineStage.ENTITIES.value, {}).get("status")
        == StageStatus.COMPLETED.value
    )


# ---------------------------------------------------------------------------
# mark_failed_with_cascade
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mark_failed_with_cascade_propagates(db_session):
    """mark_failed_with_cascade(EXTRACT) must fail EXTRACT and all per-doc downstream
    stages (METADATA, ENRICH, RELATIONSHIPS, CLAIMS, ENTITIES)
    but leave BATCH_ANALYSIS (not in EXTRACT's downstream) as PENDING.
    """
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import initialize, mark_failed_with_cascade

    case = Case(
        id="_T5", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_T5",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=True, db=db_session)
    db_session.commit()
    db_session.refresh(doc)

    mark_failed_with_cascade(doc.id, PipelineStage.EXTRACT, db_session, error="boom")

    db_session.refresh(doc)
    stages = stages_dict(doc)

    assert stages[PipelineStage.EXTRACT.value]["status"] == StageStatus.FAILED.value
    assert stages[PipelineStage.METADATA.value]["status"] == StageStatus.FAILED.value
    assert stages[PipelineStage.ENRICH.value]["status"] == StageStatus.FAILED.value
    assert (
        stages[PipelineStage.RELATIONSHIPS.value]["status"] == StageStatus.FAILED.value
    )
    assert stages[PipelineStage.CLAIMS.value]["status"] == StageStatus.FAILED.value
    assert stages[PipelineStage.ENTITIES.value]["status"] == StageStatus.FAILED.value
    # BATCH_ANALYSIS is not in EXTRACT's downstream — must stay PENDING
    assert (
        stages[PipelineStage.BATCH_ANALYSIS.value]["status"]
        == StageStatus.PENDING.value
    )
    # Downstream error messages reference the upstream stage
    assert "extract" in stages[PipelineStage.METADATA.value]["error"]


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


# ---------------------------------------------------------------------------
# RETRYING stage status — keeps polling alive across Celery retries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_overall_state_treats_retrying_as_running():
    """A retrying stage must roll up to PipelineState.RUNNING so polling
    templates (gated on pending|running) keep refreshing through the
    retry countdown."""
    from app.services.pipeline_status import compute_overall_state

    stages = {
        "extract": {"status": StageStatus.RETRYING.value},
        "metadata": {"status": StageStatus.PENDING.value},
    }
    assert compute_overall_state(stages) == PipelineState.RUNNING

    # Retrying mixed with running still rolls up to running.
    stages = {
        "extract": {"status": StageStatus.RUNNING.value},
        "metadata": {"status": StageStatus.RETRYING.value},
    }
    assert compute_overall_state(stages) == PipelineState.RUNNING


@pytest.mark.unit
def test_mark_retrying_writes_record_shape(db_session):
    """mark_retrying writes status=retrying plus error, attempt, max_attempts, next_at."""
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        mark_retrying,
        mark_started,
    )

    case = Case(
        id="_TR_R1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_TR_R1",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()

    from datetime import datetime

    next_at_dt = datetime(2026, 5, 6, 18, 32, 11)

    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    mark_retrying(
        doc.id,
        PipelineStage.EXTRACT,
        db_session,
        error="boom",
        attempt=2,
        max_attempts=3,
        next_at=next_at_dt,
    )

    db_session.refresh(doc)
    rec = stages_dict(doc)[PipelineStage.EXTRACT.value]
    assert rec["status"] == StageStatus.RETRYING.value
    assert rec["error"] == "boom"
    assert rec["attempt"] == 2
    assert rec["max_attempts"] == 3
    assert rec["next_at"] == next_at_dt.isoformat()
    # Aggregate state is RUNNING — polling must keep firing.
    assert doc.pipeline_state.value == PipelineState.RUNNING.value


@pytest.mark.unit
def test_mark_started_clears_retry_bookkeeping(db_session):
    """When the next attempt actually runs, mark_started must wipe
    attempt/max_attempts/next_at left over from a prior RETRYING record so
    the UI doesn't show stale countdown info next to a clean RUNNING dot."""
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        mark_retrying,
        mark_started,
    )

    case = Case(
        id="_TR_R2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_TR_R2",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()

    from datetime import datetime

    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    mark_retrying(
        doc.id,
        PipelineStage.EXTRACT,
        db_session,
        error="boom",
        attempt=1,
        max_attempts=3,
        next_at=datetime(2026, 5, 6, 18, 32, 11),
    )
    # Next attempt actually starts.
    mark_started(doc.id, PipelineStage.EXTRACT, db_session)

    db_session.refresh(doc)
    rec = stages_dict(doc)[PipelineStage.EXTRACT.value]
    assert rec["status"] == StageStatus.RUNNING.value
    assert "attempt" not in rec
    assert "max_attempts" not in rec
    assert "next_at" not in rec


@pytest.mark.unit
def test_schedule_retry_computes_next_at_from_countdown(db_session):
    """schedule_retry is the canonical helper used at every Celery `self.retry`
    site — it computes next_at = now + countdown and forwards to mark_retrying."""
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        mark_started,
        schedule_retry,
    )

    case = Case(
        id="_TR_R3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="x",
        content="x",
        case_id="_TR_R3",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()

    mark_started(doc.id, PipelineStage.EXTRACT, db_session)
    before = datetime.now(UTC).replace(tzinfo=None)
    schedule_retry(
        doc.id,
        PipelineStage.EXTRACT,
        db_session,
        error="timeout",
        attempt=1,
        max_attempts=3,
        countdown=60,
    )
    after = datetime.now(UTC).replace(tzinfo=None)

    db_session.refresh(doc)
    rec = stages_dict(doc)[PipelineStage.EXTRACT.value]
    assert rec["status"] == StageStatus.RETRYING.value
    next_at = datetime.fromisoformat(rec["next_at"])
    # next_at should be ~60s from now, between before+60 and after+60
    assert (
        before + timedelta(seconds=60)
        <= next_at
        <= after + timedelta(seconds=60, microseconds=1)
    )


@pytest.mark.unit
def test_recover_orphaned_running_stages_resets_retrying(db_session):
    """A worker that died between mark_retrying and the next Celery attempt
    leaves the stage stuck in RETRYING forever. App-startup recovery must
    treat RETRYING the same as RUNNING — reset to PENDING and re-dispatch."""
    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_orphaned_running_stages,
    )

    case = Case(
        id="_TR_R4", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="stuck-retrying.pdf",
        content="x",
        case_id="_TR_R4",
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    from datetime import datetime as _dt

    from sqlalchemy import text as _text

    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages SET status=:status, error=:err, "
            "attempt=:att, max_attempts=:max, next_at=:nxt "
            "WHERE document_id=:id AND stage=:stage"
        ),
        {
            "status": StageStatus.RETRYING.value,
            "err": "boom",
            "att": 2,
            "max": 3,
            "nxt": _dt(2026, 5, 6, 18, 32, 11),
            "id": doc.id,
            "stage": PipelineStage.EXTRACT.value,
        },
    )
    doc.pipeline_state = "running"  # retrying rolls up to running
    db_session.expire(doc, ["stage_rows"])
    db_session.commit()

    # Test calls with min_age_seconds=0 (startup mode): no time threshold,
    # reset everything. The cron path uses the default 1200s threshold.
    result = recover_orphaned_running_stages(db_session, min_age_seconds=0)

    assert result["docs_reset"] == 1
    assert result["stages_reset"] == 1

    db_session.refresh(doc)
    rec = stages_dict(doc)[PipelineStage.EXTRACT.value]
    assert rec["status"] == StageStatus.PENDING.value
    # Retry bookkeeping should be wiped on reset.
    assert "attempt" not in rec
    assert "max_attempts" not in rec
    assert "next_at" not in rec


@pytest.mark.unit
def test_recover_orphaned_skips_recently_started_stages(db_session):
    """A stage that started < min_age_seconds ago is presumed legitimately
    running, not orphaned. The cron-mode recovery must NOT kill it.

    Reproduces the batch_33-ran-twice bug: a 5-min cron tick fired
    mid-batch_analysis (which routinely runs 4-5 min), reset the stage,
    cleared analysis_queued_at, and let recover_unclaimed_ready_batches
    dispatch a duplicate analyzer."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as _text

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_orphaned_running_stages,
    )

    case = Case(
        id="_TR_R5", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="legitimately-running.pdf",
        content="x",
        case_id="_TR_R5",
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    # initialize() resets pipeline_state to PENDING; set it back to running
    # AFTER initialize to simulate a doc whose batch_analysis is mid-flight.
    doc.pipeline_state = "running"

    # Stage started 30 seconds ago — well within the 1200s default threshold.
    recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages SET status=:status, started_at=:started "
            "WHERE document_id=:id AND stage=:stage"
        ),
        {
            "status": StageStatus.RUNNING.value,
            "started": recent,
            "id": doc.id,
            "stage": PipelineStage.BATCH_ANALYSIS.value,
        },
    )
    db_session.expire(doc, ["stage_rows"])
    db_session.commit()

    # Cron-mode call (default min_age_seconds=1200) must skip the recent stage.
    result = recover_orphaned_running_stages(db_session)
    assert result["docs_reset"] == 0
    assert result["stages_reset"] == 0

    # Same call with min_age_seconds=0 (startup mode) does reset it.
    result_startup = recover_orphaned_running_stages(db_session, min_age_seconds=0)
    assert result_startup["docs_reset"] == 1
    assert result_startup["stages_reset"] == 1


@pytest.mark.unit
def test_recover_orphaned_skips_running_stage_when_workers_active(
    db_session, monkeypatch
):
    """A stage stuck in RUNNING past min_age_seconds must NOT be reset when
    other workers are showing forward progress within the activity window.
    Reproduces the ai-queue runaway loop: enrich_document_tasks legitimately
    gate-wait for Qwen under shared-GPU contention, individual stages can
    exceed 20 min in RUNNING while the cascade is alive elsewhere. Without
    this gate, the cron resets them, recover_stuck_pending_dispatches
    re-dispatches, the cycle repeats."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as _text

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_orphaned_running_stages,
    )

    case = Case(
        id="_TR_R6", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    # Doc A: stale RUNNING enrich (30 min ago — past 20-min threshold).
    stale = Document(
        title="stale-running.pdf",
        content="x",
        case_id="_TR_R6",
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(stale)
    db_session.flush()
    initialize(stale, batched=False, db=db_session)
    stale.pipeline_state = "running"
    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages SET status=:status, started_at=:started "
            "WHERE document_id=:id AND stage=:stage"
        ),
        {
            "status": StageStatus.RUNNING.value,
            "started": datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30),
            "id": stale.id,
            "stage": PipelineStage.ENRICH.value,
        },
    )

    # Doc B: a different doc whose batch_analysis just completed — proves
    # the worker is alive and the stale RUNNING is presumed gate-waiting.
    alive = Document(
        title="recently-active.pdf",
        content="x",
        case_id="_TR_R6",
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(alive)
    db_session.flush()
    initialize(alive, batched=False, db=db_session)
    alive.pipeline_state = "partial"
    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages "
            "SET status=:status, completed_at=:done "
            "WHERE document_id=:id AND stage=:stage"
        ),
        {
            "status": StageStatus.COMPLETED.value,
            "done": datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
            "id": alive.id,
            "stage": PipelineStage.BATCH_ANALYSIS.value,
        },
    )
    db_session.expire(stale, ["stage_rows"])
    db_session.expire(alive, ["stage_rows"])
    db_session.commit()

    result = recover_orphaned_running_stages(db_session)

    # Stale stage must NOT be reset — workers are alive.
    assert result["docs_reset"] == 0
    assert result["stages_reset"] == 0


@pytest.mark.unit
def test_recover_orphaned_resets_stale_running_when_workers_idle(db_session):
    """When no other stage has shown recent activity, the stale RUNNING is
    genuinely orphaned and recovery must reset it. Keeps the legitimate
    crash-recovery path working."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as _text

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_orphaned_running_stages,
    )

    case = Case(
        id="_TR_R7", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    # Single doc with one stale RUNNING enrich; nothing else has any activity.
    # The activity probe finds the stale row itself, but its started_at is OLD,
    # so it does NOT count as "recent activity" — recovery proceeds.
    doc = Document(
        title="genuinely-orphaned.pdf",
        content="x",
        case_id="_TR_R7",
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    doc.pipeline_state = "running"
    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages SET status=:status, started_at=:started "
            "WHERE document_id=:id AND stage=:stage"
        ),
        {
            "status": StageStatus.RUNNING.value,
            "started": datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2),
            "id": doc.id,
            "stage": PipelineStage.ENRICH.value,
        },
    )
    db_session.expire(doc, ["stage_rows"])
    db_session.commit()

    result = recover_orphaned_running_stages(db_session)

    assert result["docs_reset"] == 1
    assert result["stages_reset"] == 1


# ---------------------------------------------------------------------------
# recover_stuck_pending_dispatches — EAGER+reload hazard recovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recover_stuck_pending_redispatches_old_pending_doc(db_session, monkeypatch):
    """Doc with pipeline_state=pending + extract.status=pending older than the
    threshold must be re-dispatched. Reproduces the ib-0007 stuck-upload case
    where uvicorn --reload killed the daemon thread before mark_started ran."""
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="stuck.pdf",
        content=None,  # Docling never ran
        case_id="_TR1",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()
    db_session.refresh(doc)

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        # task is a Celery task object; we only assert it was called with the doc id
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session)

    assert result["docs_redispatched"] == 1
    assert result["doc_ids"] == [doc.id]
    assert captured == [doc.id]


@pytest.mark.unit
def test_recover_stuck_pending_skips_recent_pending_doc(db_session, monkeypatch):
    """A pending doc inside the age threshold (recently uploaded) must NOT be
    re-dispatched — the upload's own dispatch is still in flight."""
    from datetime import UTC, datetime

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="fresh.pdf",
        content=None,
        case_id="_TR2",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None),  # just now
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session, max_age_seconds=60)

    assert result["docs_redispatched"] == 0
    assert result["doc_ids"] == []
    assert captured == []


@pytest.mark.unit
def test_recover_stuck_pending_resumes_partial_pipeline(db_session, monkeypatch):
    """A doc with EXTRACT completed but METADATA pending (and old) must be
    re-dispatched — the cascade got killed mid-flight, not before it started.
    Reproduces the ib-0007 follow-up bug where recovery silently skipped
    docs whose EXTRACT had already finished."""
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        PipelineStage,
        StageStatus,
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="extracted.pdf",
        content="some content",
        case_id="_TR3",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    from sqlalchemy import text as _text

    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages SET status=:status "
            "WHERE document_id=:id AND stage=:stage"
        ),
        {
            "status": StageStatus.COMPLETED.value,
            "id": doc.id,
            "stage": PipelineStage.EXTRACT.value,
        },
    )
    doc.pipeline_state = "partial"  # extract done, downstream pending
    db_session.expire(doc, ["stage_rows"])
    db_session.commit()

    captured: list[tuple] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append((task, args, kwargs))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session)

    # Recovery must dispatch METADATA's retry_task (metadata_task on the ai
    # queue) so the doc resumes from METADATA — EXTRACT already finished and
    # the ingest-queue task only owns EXTRACT.
    assert result["docs_redispatched"] == 1
    assert result["doc_ids"] == [doc.id]
    assert len(captured) == 1
    task, args, _ = captured[0]
    assert args == (doc.id,)
    assert task.name == "app.tasks.document_processing.metadata_task"


@pytest.mark.unit
def test_recover_stuck_pending_skips_running_stage(db_session, monkeypatch):
    """A doc with a RUNNING stage must NOT be re-dispatched — the running-state
    recovery owns that case (and runs first in lifespan)."""
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        PipelineStage,
        StageStatus,
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR4", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="running.pdf",
        content="x",
        case_id="_TR4",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    from sqlalchemy import text as _text

    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages SET status=:status "
            "WHERE document_id=:id AND stage=:stage"
        ),
        {
            "status": StageStatus.RUNNING.value,
            "id": doc.id,
            "stage": PipelineStage.METADATA.value,
        },
    )
    doc.pipeline_state = "running"
    db_session.expire(doc, ["stage_rows"])
    db_session.commit()

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session)

    assert result["docs_redispatched"] == 0
    assert captured == []


@pytest.mark.unit
def test_recover_stuck_pending_skips_extract_when_worker_recently_active(
    db_session, monkeypatch
):
    """When another doc's EXTRACT completed recently, the ingest worker is
    presumed alive and PENDING extracts are queue-waiting — not lost. Don't
    re-dispatch them. Reproduces the runaway-loop incident: a batch of 38 docs
    serialized through Chandra; recovery cron saw waiting docs as stuck every
    5 min and re-dispatched, racking up 900+ duplicate Chandra calls."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as _text

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services.pipeline_status import (
        StageStatus,
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR5", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    # The doc the worker just finished — proves the worker is alive. Mark
    # all its stages as completed so it doesn't itself qualify as a recovery
    # candidate (we only want to test the gate for `waiting`).
    finished = Document(
        title="finished.pdf",
        content="ok",
        case_id="_TR5",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10),
        pipeline_state="completed",
    )
    db_session.add(finished)
    db_session.flush()
    initialize(finished, batched=False, db=db_session)
    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages "
            "SET status=:status, completed_at=:ts "
            "WHERE document_id=:id"
        ),
        {
            "status": StageStatus.COMPLETED.value,
            "ts": datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
            "id": finished.id,
        },
    )
    db_session.commit()

    # The doc that's queue-waiting behind a slow predecessor — looks "stuck"
    # to the old recovery but is actually fine.
    waiting = Document(
        title="waiting.pdf",
        content=None,
        case_id="_TR5",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(waiting)
    db_session.flush()
    initialize(waiting, batched=False, db=db_session)
    db_session.commit()

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stuck_pending_dispatches(db_session)

    assert result["docs_redispatched"] == 0
    assert captured == []


@pytest.mark.unit
def test_recover_stuck_pending_uses_claim_stage_to_avoid_double_dispatch(
    db_session, monkeypatch
):
    """Regression for doc_39 / 2026-05-26 22:00-22:12 triple-dispatch incident.

    Setup: a doc whose head pending stage is METADATA (looks recoverable).
    Simulate a concurrent dispatcher having just claimed METADATA — meaning
    claim_stage_for_dispatch returns False because the SQL CAS sees the row
    is no longer PENDING.

    Pre-fix: recovery called dispatch_task() unconditionally → race-doubled
    extract task. Post-fix: recovery calls claim_stage_for_dispatch first
    and skips dispatch when the claim fails.
    """
    from datetime import UTC, datetime, timedelta

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services import pipeline_status as ps
    from app.services.pipeline_status import (
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR_RACE",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="raced.pdf",
        content=None,
        case_id="_TR_RACE",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    # Simulate another worker having just claimed the head stage: the SQL
    # CAS would have transitioned PENDING→RUNNING, leaving no PENDING row to
    # claim. We monkeypatch claim_stage_for_dispatch to return False directly
    # so the test exercises only the recovery's gate logic.
    monkeypatch.setattr(
        ps, "claim_stage_for_dispatch", lambda _doc_id, _stage, _db: False
    )

    result = recover_stuck_pending_dispatches(db_session)

    assert result["docs_redispatched"] == 0, (
        "recovery must not double-dispatch when claim_stage_for_dispatch fails"
    )
    assert captured == [], "dispatch_task must not be called when claim fails"


@pytest.mark.unit
def test_recover_stuck_pending_dispatches_metadata_without_preclaim(
    db_session, monkeypatch
):
    """Regression for the ib-0001 recovery deadlock.

    metadata_task self-claims METADATA on entry. If recovery pre-claims it
    (PENDING→RUNNING) and then dispatches, the task self-claims, sees RUNNING,
    and skips as 'already_claimed' — a permanent deadlock. The fix marks
    METADATA self_claims=True so recovery dispatches it UNCLAIMED.

    We monkeypatch claim_stage_for_dispatch to ALWAYS fail: pre-fix that would
    have skipped the dispatch; post-fix metadata is dispatched anyway because
    its branch never calls the pre-claim.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as _text

    from app.models.database import Case, Document
    from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
    from app.services import pipeline_status as ps
    from app.services.pipeline_status import (
        StageStatus,
        initialize,
        recover_stuck_pending_dispatches,
    )

    case = Case(
        id="_TR_SC", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="stuck.pdf",
        content="x",
        case_id="_TR_SC",
        originator_type=OriginatorType.UNKNOWN,
        ingest_date=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.execute(
        _text(
            "UPDATE document_pipeline_stages SET status=:s "
            "WHERE document_id=:id AND stage=:stage"
        ),
        {"s": StageStatus.COMPLETED.value, "id": doc.id, "stage": "extract"},
    )
    doc.pipeline_state = "partial"  # extract done → METADATA is the head
    db_session.expire(doc, ["stage_rows"])
    db_session.commit()

    captured: list[tuple] = []
    monkeypatch.setattr(
        "app.tasks.dispatch.dispatch_task",
        lambda task, *a, **k: captured.append((task, a)),
    )
    # If recovery were to pre-claim METADATA, this False would skip dispatch.
    monkeypatch.setattr(
        ps, "claim_stage_for_dispatch", lambda _doc_id, _stage, _db: False
    )

    result = recover_stuck_pending_dispatches(db_session)

    assert result["docs_redispatched"] == 1
    assert len(captured) == 1
    task, args = captured[0]
    assert task.name == "app.tasks.document_processing.metadata_task"
    assert args == (doc.id,)
    # METADATA must be left PENDING (not pre-claimed to RUNNING) for the task
    # to self-claim when it runs.
    row = db_session.execute(
        _text(
            "SELECT status FROM document_pipeline_stages "
            "WHERE document_id=:id AND stage='metadata'"
        ),
        {"id": doc.id},
    ).scalar()
    assert row == StageStatus.PENDING.value


@pytest.mark.unit
def test_only_metadata_self_claims_in_registry():
    """Guard: METADATA is the only self-claiming stage (others mark_started)."""
    from app.services.pipeline_status import STAGE_REGISTRY, PipelineStage

    self_claiming = {s for s, spec in STAGE_REGISTRY.items() if spec.self_claims}
    assert self_claiming == {PipelineStage.METADATA}


# ---------------------------------------------------------------------------
# recover_unclaimed_ready_batches — closes the gap where per-stage retries
# leave an IngestBatch with analysis_queued_at IS NULL but every doc ready.
# ---------------------------------------------------------------------------


def _seed_batch_with_docs(db_session, *, case_id: str, doc_stages: list[dict]):
    """Create an IngestBatch + Documents whose pipeline_stages match `doc_stages`.

    Each entry in `doc_stages` is a dict mapping PipelineStage.value → status string.
    Stages not listed default to PENDING via initialize().
    """
    from sqlalchemy import text as _text

    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchSourceType, OriginatorType
    from app.services.pipeline_status import initialize

    batch = IngestBatch(
        source_type=IngestBatchSourceType.MANUAL,
        case_id=None,
        analysis_queued_at=None,
    )
    db_session.add(batch)
    db_session.flush()

    docs = []
    for i, stage_overrides in enumerate(doc_stages):
        doc = Document(
            title=f"d{i}.pdf",
            content="x",
            case_id=case_id,
            originator_type=OriginatorType.UNKNOWN,
            ingest_batch_id=batch.id,
        )
        db_session.add(doc)
        db_session.flush()
        initialize(doc, batched=True, db=db_session)
        for stage_key, status in stage_overrides.items():
            db_session.execute(
                _text(
                    "UPDATE document_pipeline_stages SET status=:status "
                    "WHERE document_id=:id AND stage=:stage"
                ),
                {"status": status, "id": doc.id, "stage": stage_key},
            )
        db_session.expire(doc, ["stage_rows"])
        docs.append(doc)
    db_session.commit()
    db_session.refresh(batch)
    for d in docs:
        db_session.refresh(d)
    return batch, docs


@pytest.mark.unit
def test_recover_unclaimed_ready_batches_claims_and_dispatches(db_session, monkeypatch):
    """Batch where every doc has extract+metadata completed and batch_analysis
    still pending must be claimed (analysis_queued_at set) and analyze_batch_task
    dispatched."""
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction
    from app.services.pipeline_status import (
        initialize,  # noqa: F401  — used inside _seed helper
        recover_unclaimed_ready_batches,
    )

    case = Case(
        id="_RU1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    ready = {
        PipelineStage.EXTRACT.value: StageStatus.COMPLETED.value,
        PipelineStage.METADATA.value: StageStatus.COMPLETED.value,
    }
    batch, _docs = _seed_batch_with_docs(
        db_session, case_id="_RU1", doc_stages=[ready, ready]
    )

    captured: list[int] = []

    class _FakeDelay:
        # recover_unclaimed_ready_batches now goes through dispatch_task,
        # which calls apply_async; keep delay for any direct callers.
        def delay(self, batch_id):
            captured.append(batch_id)

        def apply_async(self, args=(), kwargs=None):
            captured.append(args[0])

    monkeypatch.setattr("app.tasks.analyze_batch.analyze_batch_task", _FakeDelay())

    result = recover_unclaimed_ready_batches(db_session)

    assert result["batches_dispatched"] == 1
    assert result["batch_ids"] == [batch.id]
    assert captured == [batch.id]

    db_session.refresh(batch)
    assert batch.analysis_queued_at is not None


@pytest.mark.unit
def test_recover_unclaimed_ready_batches_skips_when_metadata_pending(
    db_session, monkeypatch
):
    """Readiness must be honoured: if any doc still has metadata pending, the
    claim's NOT EXISTS guard rejects, no dispatch happens, analysis_queued_at
    stays NULL. Proves we delegate readiness to claim_batch_for_analysis()."""
    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction
    from app.services.pipeline_status import (
        initialize,  # noqa: F401
        recover_unclaimed_ready_batches,
    )

    case = Case(
        id="_RU2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    ready = {
        PipelineStage.EXTRACT.value: StageStatus.COMPLETED.value,
        PipelineStage.METADATA.value: StageStatus.COMPLETED.value,
    }
    not_ready = {
        PipelineStage.EXTRACT.value: StageStatus.COMPLETED.value,
        # METADATA still pending — batch should NOT be claimed
    }
    batch, _docs = _seed_batch_with_docs(
        db_session, case_id="_RU2", doc_stages=[ready, not_ready]
    )

    captured: list[int] = []

    class _FakeDelay:
        # recover_unclaimed_ready_batches now goes through dispatch_task,
        # which calls apply_async; keep delay for any direct callers.
        def delay(self, batch_id):
            captured.append(batch_id)

        def apply_async(self, args=(), kwargs=None):
            captured.append(args[0])

    monkeypatch.setattr("app.tasks.analyze_batch.analyze_batch_task", _FakeDelay())

    result = recover_unclaimed_ready_batches(db_session)

    assert result["batches_dispatched"] == 0
    assert result["batch_ids"] == []
    assert captured == []

    db_session.refresh(batch)
    assert batch.analysis_queued_at is None


@pytest.mark.unit
def test_recover_unclaimed_ready_batches_ignores_already_claimed(
    db_session, monkeypatch
):
    """A batch with analysis_queued_at already set must be ignored — the
    inline trigger or a prior tick already handled it. Idempotency guard."""
    from datetime import UTC, datetime

    from app.models.database import Case
    from app.models.enums import CaseStatus, Jurisdiction
    from app.services.pipeline_status import (
        initialize,  # noqa: F401
        recover_unclaimed_ready_batches,
    )

    case = Case(
        id="_RU3", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    ready = {
        PipelineStage.EXTRACT.value: StageStatus.COMPLETED.value,
        PipelineStage.METADATA.value: StageStatus.COMPLETED.value,
    }
    batch, _docs = _seed_batch_with_docs(db_session, case_id="_RU3", doc_stages=[ready])
    claimed_at = datetime.now(UTC).replace(tzinfo=None)
    batch.analysis_queued_at = claimed_at
    db_session.commit()

    captured: list[int] = []

    class _FakeDelay:
        # recover_unclaimed_ready_batches now goes through dispatch_task,
        # which calls apply_async; keep delay for any direct callers.
        def delay(self, batch_id):
            captured.append(batch_id)

        def apply_async(self, args=(), kwargs=None):
            captured.append(args[0])

    monkeypatch.setattr("app.tasks.analyze_batch.analyze_batch_task", _FakeDelay())

    result = recover_unclaimed_ready_batches(db_session)

    assert result["batches_dispatched"] == 0
    assert captured == []

    db_session.refresh(batch)
    # Claim timestamp unchanged.
    assert batch.analysis_queued_at == claimed_at


# ---------------------------------------------------------------------------
# retry_on_db_locked
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retry_on_db_locked_succeeds_on_first_try(db_session):
    """fn that succeeds immediately: returns value, rollback never called."""
    from unittest.mock import Mock

    from app.services.pipeline_status import retry_on_db_locked

    fn = Mock(return_value="ok")
    db_session.rollback = Mock()

    result = retry_on_db_locked(fn, db_session)

    assert result == "ok"
    fn.assert_called_once()
    db_session.rollback.assert_not_called()


@pytest.mark.unit
def test_retry_on_db_locked_retries_then_succeeds(db_session, monkeypatch):
    """fn raises 'database is locked' once then succeeds: retries, rolls back once."""
    from unittest.mock import Mock

    from sqlalchemy.exc import OperationalError

    from app.services.pipeline_status import retry_on_db_locked

    locked_exc = OperationalError("stmt", {}, Exception("database is locked"))
    fn = Mock(side_effect=[locked_exc, "ok"])
    db_session.rollback = Mock()
    sleep_mock = Mock()
    monkeypatch.setattr("app.services.pipeline_status.time.sleep", sleep_mock)

    result = retry_on_db_locked(fn, db_session)

    assert result == "ok"
    assert fn.call_count == 2
    db_session.rollback.assert_called_once()
    sleep_mock.assert_called_once()
    sleep_args, _ = sleep_mock.call_args
    assert sleep_args[0] > 0


@pytest.mark.unit
def test_retry_on_db_locked_reraises_non_locked_error(db_session):
    """Non-locked OperationalError is re-raised immediately, before rollback."""
    from unittest.mock import Mock

    import pytest as _pytest
    from sqlalchemy.exc import OperationalError

    from app.services.pipeline_status import retry_on_db_locked

    fn = Mock(
        side_effect=OperationalError("stmt", {}, Exception("UNIQUE constraint failed"))
    )
    db_session.rollback = Mock()

    with _pytest.raises(OperationalError):
        retry_on_db_locked(fn, db_session)

    fn.assert_called_once()
    db_session.rollback.assert_not_called()


@pytest.mark.unit
def test_retry_on_db_locked_exhausts_and_reraises(db_session, monkeypatch):
    """fn always raises 'database is locked': exhausts attempts and re-raises."""
    from unittest.mock import Mock

    import pytest as _pytest
    from sqlalchemy.exc import OperationalError

    from app.services.pipeline_status import retry_on_db_locked

    locked_exc = OperationalError("stmt", {}, Exception("database is locked"))
    fn = Mock(side_effect=locked_exc)
    db_session.rollback = Mock()
    sleep_mock = Mock()
    monkeypatch.setattr("app.services.pipeline_status.time.sleep", sleep_mock)

    with _pytest.raises(OperationalError):
        retry_on_db_locked(fn, db_session, attempts=2)

    assert fn.call_count == 2
    assert db_session.rollback.call_count == 2


# ---------------------------------------------------------------------------
# SQL injection hardening — _update_stage extra_sets whitelist
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_update_stage_rejects_disallowed_key(db_session):
    """_update_stage raises ValueError for unknown extra_sets keys (SQL injection guard)."""
    from unittest.mock import MagicMock

    from app.services.pipeline_status import PipelineStage, StageStatus, _update_stage

    db = MagicMock()
    with pytest.raises(ValueError, match="disallowed extra_sets key"):
        _update_stage(
            doc_id=1,
            stage=PipelineStage.ENRICH,
            db=db,
            status=StageStatus.COMPLETED,
            extra_sets={"'; DROP TABLE documents; --": "evil"},
        )
    # db.execute should NOT have been called
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# recover_stranded_batch_pending
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recover_stranded_batch_pending_promotes_and_dispatches(
    db_session, monkeypatch
):
    """Single-doc metadata retry in an already-analyzed batch leaves the retried
    doc with batch_analysis=pending while its siblings have batch_analysis=completed.
    recover_stranded_batch_pending must promote it to completed and dispatch enrich.

    Setup mirrors the real scenario:
    - Batch with doc A (sibling, batch_analysis=completed) and doc B (retried,
      batch_analysis=pending from cascade reset, enrich=pending).
    - Recovery sweep must: mark B.batch_analysis=completed, dispatch enrich for B.
    """
    from app.models.database import Case, Document, IngestBatch
    from app.models.enums import (
        CaseStatus,
        IngestBatchSourceType,
        IngestBatchStatus,
        Jurisdiction,
        OriginatorType,
    )
    from app.services.pipeline_status import (
        StageStatus,
        initialize,
        mark_completed,
        recover_stranded_batch_pending,
        stages_dict,
    )

    case = Case(
        id="_TBP1", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=case.id,
        status=IngestBatchStatus.PENDING,
        received_at=__import__("datetime").datetime.now(),
        ingest_date=__import__("datetime").datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    doc_a = Document(
        title="sibling.pdf",
        content="x",
        case_id=case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    doc_b = Document(
        title="retried.pdf",
        content="x",
        case_id=case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add_all([doc_a, doc_b])
    db_session.flush()

    # Both docs get pipeline stages initialized.
    initialize(doc_a, batched=True, db=db_session)
    initialize(doc_b, batched=True, db=db_session)
    db_session.commit()

    # Doc A has already completed batch_analysis (the normal case).
    mark_completed(doc_a.id, PipelineStage.BATCH_ANALYSIS, db_session)

    # Doc B simulates the retried state: batch_analysis=pending, enrich=pending.
    # (reset_stage would have done this cascade; we set it up manually here.)
    # batch_analysis is already pending from initialize(); enrich too.

    # Sanity-check setup.
    db_session.expire_all()
    stages_b = stages_dict(doc_b)
    assert (
        stages_b[PipelineStage.BATCH_ANALYSIS.value]["status"]
        == StageStatus.PENDING.value
    )
    assert stages_b[PipelineStage.ENRICH.value]["status"] == StageStatus.PENDING.value

    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stranded_batch_pending(db_session)

    assert result["docs_recovered"] == 1
    assert doc_b.id in result["doc_ids"]
    assert captured == [doc_b.id], "enrich must be dispatched for the stranded doc"

    # Doc B's batch_analysis must now be completed.
    db_session.expire_all()
    stages_b_after = stages_dict(doc_b)
    assert (
        stages_b_after[PipelineStage.BATCH_ANALYSIS.value]["status"]
        == StageStatus.COMPLETED.value
    ), "batch_analysis must be promoted to completed"

    # Doc A must not have been touched.
    stages_a_after = stages_dict(doc_a)
    assert (
        stages_a_after[PipelineStage.BATCH_ANALYSIS.value]["status"]
        == StageStatus.COMPLETED.value
    )


@pytest.mark.unit
def test_recover_stranded_batch_pending_ignores_single_doc_batches(
    db_session, monkeypatch
):
    """A single-doc batch where the doc itself has batch_analysis=pending is NOT
    stranded — it's still waiting for the normal analyze_batch_task. The sweep
    must not promote it."""
    from app.models.database import Case, Document, IngestBatch
    from app.models.enums import (
        CaseStatus,
        IngestBatchSourceType,
        IngestBatchStatus,
        Jurisdiction,
        OriginatorType,
    )
    from app.services.pipeline_status import (
        StageStatus,
        initialize,
        recover_stranded_batch_pending,
        stages_dict,
    )

    case = Case(
        id="_TBP2", title="T", status=CaseStatus.INTAKE, jurisdiction=Jurisdiction.DE
    )
    db_session.add(case)
    db_session.commit()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=case.id,
        status=IngestBatchStatus.PENDING,
        received_at=__import__("datetime").datetime.now(),
        ingest_date=__import__("datetime").datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="solo.pdf",
        content="x",
        case_id=case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=True, db=db_session)
    db_session.commit()

    # No sibling has batch_analysis=completed — sweep must leave this alone.
    captured: list[int] = []

    def fake_dispatch(task, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("doc_id"))

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", fake_dispatch)

    result = recover_stranded_batch_pending(db_session)

    assert result["docs_recovered"] == 0
    assert captured == []
    stages = stages_dict(doc)
    assert (
        stages[PipelineStage.BATCH_ANALYSIS.value]["status"]
        == StageStatus.PENDING.value
    )
