"""History gate: RELATIONSHIPS waits for earlier documents of the case to be enriched."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.database import (
    Case,
    Document,
    DocumentPipelineStage,
    IngestBatch,
)
from app.models.enums import (
    CaseStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
    PipelineStage,
    PipelineState,
    StageStatus,
)
from app.services import pipeline_status
from app.services.pipeline_status import (
    RELATIONSHIPS_GATE_MAX_WAIT,
    RELATIONSHIPS_HOLD_REASON,
    held_relationship_doc_ids,
    hold_relationships,
    relationships_gate_open,
)
from app.tasks import enrich_document

pytestmark = pytest.mark.unit


def _doc(
    db,
    case_id,
    enrich: str = "completed",
    relationships: str = "pending",
    *,
    batch=None,
    enriched_at=None,
) -> int:
    doc = Document(
        title="d",
        case_id=case_id,
        ingest_batch_id=batch,
        ingest_date=datetime.now(UTC) - timedelta(hours=1),
    )
    db.add(doc)
    db.flush()
    pipeline_status.initialize(doc, batched=True, db=db)  # every stage PENDING
    doc.pipeline_state = PipelineState.PARTIAL
    # Every stage except the two under test is done, so nothing else can be the
    # "head" pending stage a recovery sweep would pick.
    db.query(DocumentPipelineStage).filter(
        DocumentPipelineStage.document_id == doc.id,
        DocumentPipelineStage.stage.notin_(("enrich", "relationships")),
    ).update({"status": "completed"})
    db.query(DocumentPipelineStage).filter_by(
        document_id=doc.id, stage="enrich"
    ).update({"status": enrich, "completed_at": enriched_at})
    db.query(DocumentPipelineStage).filter_by(
        document_id=doc.id, stage="relationships"
    ).update({"status": relationships})
    db.commit()
    return doc.id


def _case(db, case_id: str):
    db.add(
        Case(
            id=case_id,
            title=case_id,
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    db.commit()


# --- The predicate ------------------------------------------------------------


def test_the_first_document_of_a_case_has_nothing_to_wait_for(db_session, sample_case):
    assert relationships_gate_open(db_session, _doc(db_session, sample_case.id))


@pytest.mark.parametrize("status", ["pending", "running", "retrying"])
def test_closed_while_an_earlier_document_is_still_being_enriched(
    db_session, sample_case, status
):
    _doc(db_session, sample_case.id, enrich=status)
    later = _doc(db_session, sample_case.id)
    assert relationships_gate_open(db_session, later) is False


@pytest.mark.parametrize("status", ["completed", "failed", "skipped", "dismissed"])
def test_open_once_the_earlier_document_has_finished_enrich_either_way(
    db_session, sample_case, status
):
    _doc(db_session, sample_case.id, enrich=status)
    assert relationships_gate_open(db_session, _doc(db_session, sample_case.id))


def test_only_earlier_documents_of_the_same_case_count(db_session, sample_case):
    _case(db_session, "OTHER-1")
    mine = _doc(db_session, sample_case.id)
    _doc(db_session, "OTHER-1", enrich="running")  # another case: irrelevant
    assert relationships_gate_open(db_session, mine)

    _doc(db_session, sample_case.id, enrich="running")  # a *later* document
    assert relationships_gate_open(db_session, mine)


def test_unfiled_mail_never_waits(db_session, sample_triage_case):
    _doc(db_session, "_TRIAGE", enrich="running")
    assert relationships_gate_open(db_session, _doc(db_session, "_TRIAGE"))


def test_a_scan_awaiting_slice_review_does_not_hold_the_case(db_session, sample_case):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="s",
        status=IngestBatchStatus.AWAITING_SLICING,
    )
    db_session.add(batch)
    db_session.flush()
    _doc(db_session, sample_case.id, enrich="pending", batch=batch.id)
    assert relationships_gate_open(db_session, _doc(db_session, sample_case.id))


def test_a_stuck_predecessor_cannot_hold_the_case_forever(db_session, sample_case):
    _doc(db_session, sample_case.id, enrich="running")
    long_ago = datetime.now(UTC) - RELATIONSHIPS_GATE_MAX_WAIT - timedelta(minutes=1)
    stale = _doc(db_session, sample_case.id, enriched_at=long_ago)
    recent = _doc(db_session, sample_case.id, enriched_at=datetime.now(UTC))

    assert relationships_gate_open(db_session, stale) is True
    assert relationships_gate_open(db_session, recent) is False


# --- Holding and releasing -----------------------------------------------------


def _stage(db, doc_id, stage):
    db.expire_all()
    return (
        db.query(DocumentPipelineStage).filter_by(document_id=doc_id, stage=stage).one()
    )


def test_a_held_stage_stays_pending_and_says_why(db_session, sample_case):
    _doc(db_session, sample_case.id, enrich="running")
    later = _doc(db_session, sample_case.id)

    with patch.object(enrich_document, "_dispatch_if_pending") as dispatch:
        enrich_document._dispatch_relationships(later)

    dispatch.assert_not_called()
    row = _stage(db_session, later, "relationships")
    assert row.status == "pending" and row.reason == RELATIONSHIPS_HOLD_REASON
    assert held_relationship_doc_ids(db_session, sample_case.id) == [later]


def test_an_open_gate_dispatches_straight_away(db_session, sample_case):
    only = _doc(db_session, sample_case.id)
    with patch.object(enrich_document, "_dispatch_if_pending") as dispatch:
        enrich_document._dispatch_relationships(only)
    dispatch.assert_called_once_with(only, PipelineStage.RELATIONSHIPS)


def test_finishing_the_earlier_document_releases_the_held_ones_in_order(
    db_session, sample_case
):
    first = _doc(db_session, sample_case.id, enrich="running")
    second = _doc(db_session, sample_case.id)
    third = _doc(db_session, sample_case.id)
    for doc_id in (second, third):
        hold_relationships(db_session, doc_id)

    # While the first is still running nothing is released.
    with patch.object(enrich_document, "_dispatch_if_pending") as dispatch:
        enrich_document.release_relationship_gate(second)
    dispatch.assert_not_called()

    db_session.query(DocumentPipelineStage).filter_by(
        document_id=first, stage="enrich"
    ).update({"status": "completed"})
    db_session.commit()
    with patch.object(enrich_document, "_dispatch_if_pending") as dispatch:
        enrich_document.release_relationship_gate(first)
    assert [c.args for c in dispatch.call_args_list] == [
        (second, PipelineStage.RELATIONSHIPS),
        (third, PipelineStage.RELATIONSHIPS),
    ]


def test_a_failed_predecessor_also_releases_the_case(db_session, sample_case):
    first = _doc(db_session, sample_case.id, enrich="failed")
    held = _doc(db_session, sample_case.id)
    hold_relationships(db_session, held)

    with patch.object(enrich_document, "_dispatch_if_pending") as dispatch:
        enrich_document.release_relationship_gate(first)
    dispatch.assert_called_once_with(held, PipelineStage.RELATIONSHIPS)


def test_a_release_only_starts_a_stage_once_even_if_two_releases_race(
    db_session, sample_case
):
    _doc(db_session, sample_case.id)
    held = _doc(db_session, sample_case.id)
    hold_relationships(db_session, held)

    with patch.object(enrich_document, "_dispatch_safely") as dispatch:
        enrich_document._dispatch_if_pending(held, PipelineStage.RELATIONSHIPS)
        enrich_document._dispatch_if_pending(held, PipelineStage.RELATIONSHIPS)
    dispatch.assert_called_once()  # the pending->running claim let only one through


def test_a_started_stage_no_longer_shows_the_hold_reason(db_session, sample_case):
    held = _doc(db_session, sample_case.id)
    hold_relationships(db_session, held)

    pipeline_status.mark_started(held, PipelineStage.RELATIONSHIPS, db_session)

    row = _stage(db_session, held, "relationships")
    assert row.status == StageStatus.RUNNING.value and row.reason is None


def test_only_stages_this_gate_held_are_released(db_session, sample_case):
    _doc(db_session, sample_case.id)
    waiting_for_something_else = _doc(db_session, sample_case.id)  # pending, no reason
    held = _doc(db_session, sample_case.id)
    hold_relationships(db_session, held)
    assert held_relationship_doc_ids(db_session, sample_case.id) == [held]
    assert waiting_for_something_else not in held_relationship_doc_ids(
        db_session, sample_case.id
    )


# --- Recovery sweeper -----------------------------------------------------------


def test_the_stuck_dispatch_sweeper_leaves_a_held_stage_alone(db_session, sample_case):
    _doc(db_session, sample_case.id, enrich="running")
    held = _doc(db_session, sample_case.id)
    hold_relationships(db_session, held)

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        result = pipeline_status.recover_stuck_pending_dispatches(db_session)

    assert held not in result["doc_ids"]
    dispatch.assert_not_called()


def test_the_sweeper_starts_a_held_stage_once_the_gate_has_opened(
    db_session, sample_case
):
    first = _doc(db_session, sample_case.id, enrich="running")
    held = _doc(db_session, sample_case.id)
    hold_relationships(db_session, held)
    db_session.query(DocumentPipelineStage).filter_by(
        document_id=first, stage="enrich"
    ).update({"status": "completed"})
    db_session.commit()

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        result = pipeline_status.recover_stuck_pending_dispatches(db_session)

    assert held in result["doc_ids"]
    assert dispatch.call_args.args[1] == held


# --- Processing queue -----------------------------------------------------------


def test_the_queue_says_why_a_held_document_is_waiting(db_session, sample_case):
    from app.api.v1.worker_queue import _queue_item

    _doc(db_session, sample_case.id, enrich="running")
    held = _doc(db_session, sample_case.id)
    plain = _doc(db_session, sample_case.id)
    hold_relationships(db_session, held)

    def item(doc_id):
        doc = db_session.get(Document, doc_id)
        return _queue_item(
            {"type": "doc", "doc": doc, "stage": PipelineStage.RELATIONSHIPS}
        )

    assert item(held).note == "Waiting for earlier documents of the case"
    assert item(plain).note is None


# --- CLAIMS fans out off ENRICH, independent of the gate ----------------------


def _claims_pending(db, doc_id: int, relationships: str) -> None:
    for stage, status in (("claims", "pending"), ("relationships", relationships)):
        db.query(DocumentPipelineStage).filter_by(
            document_id=doc_id, stage=stage
        ).update({"status": status})
    db.commit()


def test_enrich_success_dispatches_claims_even_while_relationships_is_held(
    db_session, sample_case
):
    _doc(db_session, sample_case.id, enrich="running")  # closes the gate
    later = _doc(db_session, sample_case.id, enrich="running")
    _claims_pending(db_session, later, "pending")

    with (
        patch("app.dependencies.get_db_session", return_value=db_session),
        patch("app.services.intelligence.document_enricher.enrich"),
        patch.object(db_session, "close", return_value=None),
        patch.object(enrich_document, "_trigger_cost_rollup"),
        patch.object(enrich_document, "_dispatch_safely") as dispatch_safely,
    ):
        db_session.query(DocumentPipelineStage).filter_by(
            document_id=later, stage="enrich"
        ).update({"status": "pending"})
        db_session.commit()
        enrich_document.enrich_document_task.run(later)

    dispatched = {c.args[1] for c in dispatch_safely.call_args_list}
    assert PipelineStage.CLAIMS in dispatched
    assert PipelineStage.RELATIONSHIPS not in dispatched  # still held
    assert _stage(db_session, later, "relationships").reason == (
        RELATIONSHIPS_HOLD_REASON
    )


def test_the_sweeper_recovers_a_lost_claims_dispatch_behind_a_held_relationships(
    db_session, sample_case
):
    from app.tasks.extract_claims import extract_claims_task

    _doc(db_session, sample_case.id, enrich="running")  # RELATIONSHIPS stays held
    stranded = _doc(db_session, sample_case.id)
    hold_relationships(db_session, stranded)
    _claims_pending(db_session, stranded, "pending")

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        result = pipeline_status.recover_stuck_pending_dispatches(db_session)

    assert stranded in result["doc_ids"]
    assert [(c.args[0], c.args[1]) for c in dispatch.call_args_list] == [
        (extract_claims_task, stranded)
    ]


def test_retry_failed_redispatches_each_failed_sibling_stage(db_session, sample_case):
    from app.services import worker_queue

    doc_id = _doc(db_session, sample_case.id)
    _claims_pending(db_session, doc_id, "failed")
    db_session.query(DocumentPipelineStage).filter_by(
        document_id=doc_id, stage="claims"
    ).update({"status": "failed"})
    db_session.get(Document, doc_id).pipeline_state = PipelineState.FAILED
    db_session.commit()

    with (
        patch.object(
            worker_queue.access_service, "visible_case_ids", return_value=None
        ),
        patch("app.services.triage_retry.dispatch_pipeline_retry") as dispatch,
    ):
        worker_queue.retry_failed_docs_for(db_session, type("U", (), {"id": 1})())

    assert {c.args[2] for c in dispatch.call_args_list} == {
        PipelineStage.RELATIONSHIPS,
        PipelineStage.CLAIMS,
    }


def test_refresh_review_reasons_locks_the_document_row(db_session, sample_case):
    from sqlalchemy import event

    from app.services.ingestion.service import refresh_review_reasons

    doc = db_session.get(Document, _doc(db_session, sample_case.id))
    seen: list[str] = []

    def capture(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", capture)
    try:
        refresh_review_reasons(doc, db_session)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", capture)

    assert any("FOR UPDATE" in s for s in seen)
