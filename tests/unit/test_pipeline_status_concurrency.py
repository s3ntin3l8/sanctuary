"""Concurrent pipeline-stage writes must not stomp each other.

Before the json_set fix, _update_stage used read-modify-write:
  1. SELECT pipeline_stages
  2. mutate dict in Python
  3. UPDATE pipeline_stages = <full dict>

Two threads writing different stages simultaneously both read the same
initial dict, flip their own stage, and write — last writer wins, erasing
the first writer's completion. This test pins that the json_set
implementation is stomp-proof under concurrent writes.
"""

import threading

import pytest
from sqlalchemy import text

from app.models.database import Case, Document
from app.models.enums import (
    CaseStatus,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
    PipelineState,
    StageStatus,
)
from app.services.pipeline_status import initialize, mark_completed, mark_started


@pytest.fixture
def staged_doc(db_session_factory):
    """A document with all pipeline stages set to PENDING."""
    Session = db_session_factory
    db = Session()
    try:
        case = Case(
            id="CONC-001",
            title="Concurrency test case",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
        db.add(case)
        db.flush()

        doc = Document(
            title="Concurrent doc",
            content="test",
            case_id=case.id,
            originator_type=OriginatorType.COURT,
            pipeline_state=PipelineState.PENDING,
        )
        db.add(doc)
        db.flush()
        initialize(doc, batched=True, db=db)
        db.commit()
        db.refresh(doc)
        return doc.id
    finally:
        db.close()


def test_concurrent_stage_writes_do_not_stomp(staged_doc, db_session_factory):
    """Four threads each completing a different stage — all must survive in the DB."""
    doc_id = staged_doc
    Session = db_session_factory

    target_stages = [
        PipelineStage.ENRICH,
        PipelineStage.CLAIMS,
        PipelineStage.ENTITIES,
        PipelineStage.RELATIONSHIPS,
    ]

    errors: list[Exception] = []
    barrier = threading.Barrier(len(target_stages))

    def complete_stage(stage: PipelineStage) -> None:
        db = Session()
        try:
            # Read stages once before the barrier so all threads read the same
            # initial state — this is the worst case for the old RMW code.
            db.execute(
                text(
                    "SELECT stage, status FROM document_pipeline_stages WHERE document_id = :id"
                ),
                {"id": doc_id},
            ).fetchall()
            barrier.wait()  # all threads have read; now write concurrently
            mark_completed(doc_id, stage, db)
        except Exception as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [
        threading.Thread(target=complete_stage, args=(s,)) for s in target_stages
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Thread errors: {errors}"

    db = Session()
    try:
        rows = db.execute(
            text(
                "SELECT stage, status FROM document_pipeline_stages WHERE document_id = :id"
            ),
            {"id": doc_id},
        ).fetchall()
    finally:
        db.close()

    stages = {row[0]: {"status": row[1]} for row in rows}
    for stage in target_stages:
        assert stages[stage.value]["status"] == StageStatus.COMPLETED.value, (
            f"Stage {stage.value} was stomped — expected completed, got "
            f"{stages[stage.value].get('status')!r}. Full stages: {stages}"
        )


def test_concurrent_started_then_completed(staged_doc, db_session_factory):
    """mark_started followed by mark_completed from concurrent threads — final state is completed."""
    doc_id = staged_doc
    Session = db_session_factory

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def worker(stage: PipelineStage, action: str) -> None:
        db = Session()
        try:
            barrier.wait()
            if action == "start":
                mark_started(doc_id, stage, db)
            else:
                mark_completed(doc_id, stage, db)
            with lock:
                results.append(f"{stage.value}:{action}")
        except Exception as exc:
            with lock:
                results.append(f"ERROR:{exc}")
        finally:
            db.close()

    threads = [
        threading.Thread(target=worker, args=(PipelineStage.ENRICH, "start")),
        threading.Thread(target=worker, args=(PipelineStage.CLAIMS, "completed")),
        threading.Thread(target=worker, args=(PipelineStage.ENTITIES, "completed")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(r.startswith("ERROR:") for r in results), f"Errors: {results}"

    db = Session()
    try:
        rows = db.execute(
            text(
                "SELECT stage, status FROM document_pipeline_stages WHERE document_id = :id"
            ),
            {"id": doc_id},
        ).fetchall()
    finally:
        db.close()

    stages = {row[0]: {"status": row[1]} for row in rows}
    assert stages[PipelineStage.CLAIMS.value]["status"] == StageStatus.COMPLETED.value
    assert stages[PipelineStage.ENTITIES.value]["status"] == StageStatus.COMPLETED.value
