"""A packet-like email attachment holds its whole batch at the OCR->chat barrier."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.api.v1.worker_queue import _queue_item
from app.models.database import AuditLog, Document, DocumentPipelineStage, IngestBatch
from app.models.enums import (
    AuditEventType,
    IngestBatchSourceType,
    PipelineStage,
    PipelineState,
)
from app.services import pipeline_status
from app.services.ingestion import packet_hold, page_text
from app.services.intelligence.orchestrator import (
    claim_batch_for_metadata_phase,
    claim_case_brief_for_dispatch,
)
from app.services.pipeline_status import clear_extraction_stamp
from app.services.user_settings_service import get_hold_packets, set_hold_packets
from app.tasks.document_processing import metadata_task

pytestmark = pytest.mark.unit


def _batch(db, source=IngestBatchSourceType.EMAIL) -> IngestBatch:
    batch = IngestBatch(source_type=source, subject="s")
    db.add(batch)
    db.flush()
    return batch


def _doc(
    db, batch, *, held=False, extract="completed", meta=None, case_id="_TRIAGE"
) -> Document:
    doc = Document(
        title="d",
        case_id=case_id,
        ingest_batch_id=batch.id,
        ingest_date=datetime.now(UTC) - timedelta(hours=1),
        meta=meta,
        held_for_split_at=datetime.now(UTC) if held else None,
    )
    db.add(doc)
    db.flush()
    pipeline_status.initialize(doc, batched=True, db=db)
    doc.pipeline_state = PipelineState.PARTIAL
    db.query(DocumentPipelineStage).filter_by(
        document_id=doc.id, stage="extract"
    ).update({"status": extract})
    db.commit()
    return doc


def _stage(db, doc_id, stage):
    db.expire_all()
    return (
        db.query(DocumentPipelineStage).filter_by(document_id=doc_id, stage=stage).one()
    )


# --- apply ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("meta", "source", "switch", "held"),
    [
        ({"packet_starts": [3]}, IngestBatchSourceType.EMAIL, True, True),
        ({"packet_starts": [3]}, IngestBatchSourceType.EMAIL, False, False),
        ({"packet_starts": [3]}, IngestBatchSourceType.MANUAL, True, False),
        ({"packet_starts": [3]}, IngestBatchSourceType.SCAN, True, False),
        ({"packet_starts": [3], "packet_kept": True}, "email", True, False),
        ({"packet_starts": [3], "slice_range": [3, 5]}, "email", True, False),
        ({}, IngestBatchSourceType.EMAIL, True, False),  # a single letter
    ],
)
def test_apply_holds_only_a_packet_of_an_email_with_the_switch_on(
    db_session, meta, source, switch, held
):
    batch = _batch(db_session, IngestBatchSourceType(source))
    doc = _doc(db_session, batch, meta=meta)
    set_hold_packets(db_session, switch)

    assert packet_hold.apply(doc, db_session) is held
    assert (doc.held_for_split_at is not None) is held


def test_a_document_without_a_batch_is_never_held(db_session):
    doc = SimpleNamespace(meta={"packet_starts": [2]}, ingest_batch_id=None)
    assert packet_hold.apply(doc, db_session) is False


def test_the_switch_defaults_to_on_and_round_trips_with_an_audit_event(db_session):
    assert get_hold_packets(db_session) is True
    set_hold_packets(db_session, False)
    assert get_hold_packets(db_session) is False
    set_hold_packets(db_session, True)
    assert get_hold_packets(db_session) is True
    events = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.SETTINGS_INGESTION_CHANGED)
        .all()
    )
    assert [e.payload for e in events] == [
        {"hold_packets": False},
        {"hold_packets": True},
    ]


# --- the barrier --------------------------------------------------------------------


def test_the_barrier_refuses_while_a_document_is_held_and_opens_on_release(db_session):
    batch = _batch(db_session)
    held = _doc(db_session, batch, held=True)
    _doc(db_session, batch)

    assert claim_batch_for_metadata_phase(batch.id, db_session) is False

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        packet_hold.release(db_session, held)

    assert dispatch.call_count == 2  # METADATA for both documents of the batch
    db_session.expire_all()
    assert db_session.get(IngestBatch, batch.id).metadata_phase_queued_at is not None


def test_releasing_one_of_two_held_documents_keeps_the_batch_held(db_session):
    batch = _batch(db_session)
    first = _doc(db_session, batch, held=True)
    second = _doc(db_session, batch, held=True)

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        packet_hold.release(db_session, first)
        dispatch.assert_not_called()
        assert packet_hold.batch_is_held(db_session, batch.id)
        packet_hold.release(db_session, second)

    assert dispatch.call_count == 2
    assert not packet_hold.batch_is_held(db_session, batch.id)


def test_keeping_a_packet_whole_is_remembered(db_session):
    batch = _batch(db_session)
    doc = _doc(db_session, batch, held=True, meta={"packet_starts": [2]})

    with patch("app.tasks.dispatch.dispatch_task"):
        packet_hold.release(db_session, doc, kept=True)
        packet_hold.release(db_session, doc, kept=True)  # idempotent

    db_session.refresh(doc)
    assert doc.held_for_split_at is None
    assert doc.meta["packet_kept"] is True
    assert packet_hold.apply(doc, db_session) is False  # never held again


def test_release_all_frees_every_batch(db_session):
    a, b = _batch(db_session), _batch(db_session)
    _doc(db_session, a, held=True)
    _doc(db_session, b, held=True)
    _doc(db_session, b)

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        assert packet_hold.release_all(db_session) == 2

    assert dispatch.call_count == 3
    assert packet_hold.held_batch_ids(db_session) == set()


# --- the entry guard ------------------------------------------------------------------


def test_metadata_task_leaves_a_held_batch_pending(db_session):
    batch = _batch(db_session)
    _doc(db_session, batch, held=True)
    sibling = _doc(db_session, batch)

    with patch("app.tasks.document_processing._run_phase1_summary") as run:
        result = metadata_task.run(sibling.id)

    assert result == {"status": "held_for_split", "doc_id": sibling.id}
    run.assert_not_called()
    assert _stage(db_session, sibling.id, "metadata").status == "pending"


@pytest.mark.parametrize("status", ["running", "retrying"])
def test_metadata_in_flight_is_put_back_when_a_reextract_holds_the_batch(
    db_session, status
):
    batch = _batch(db_session)
    _doc(db_session, batch, held=True)
    sibling = _doc(db_session, batch)
    db_session.query(DocumentPipelineStage).filter_by(
        document_id=sibling.id, stage="metadata"
    ).update({"status": status})
    db_session.commit()

    with patch("app.tasks.document_processing._run_phase1_summary") as run:
        result = metadata_task.run(sibling.id)

    assert result["status"] == "held_for_split"
    run.assert_not_called()
    assert _stage(db_session, sibling.id, "metadata").status == "pending"

    # Released: the barrier re-dispatches it (and the sweep would too).
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        packet_hold.release_all(db_session)
    assert sibling.id in [c.args[1] for c in dispatch.call_args_list]


def test_a_run_that_finishes_after_a_hold_appeared_is_redone_later(db_session):
    batch = _batch(db_session)
    packet = _doc(db_session, batch)
    sibling = _doc(db_session, batch)

    def hold_meanwhile(doc_id):
        d = db_session.get(Document, packet.id)
        d.held_for_split_at = datetime.now(UTC)
        db_session.query(DocumentPipelineStage).filter_by(
            document_id=doc_id, stage="metadata"
        ).update({"status": "completed"})
        db_session.commit()

    with patch(
        "app.tasks.document_processing._run_phase1_summary", side_effect=hold_meanwhile
    ):
        result = metadata_task.run(sibling.id)

    assert result["status"] == "held_for_split"
    assert _stage(db_session, sibling.id, "metadata").status == "pending"


# --- clearing the stamp -----------------------------------------------------------------


def test_clearing_the_extraction_stamp_ends_the_hold(db_session):
    batch = _batch(db_session)
    doc = _doc(db_session, batch, held=True, meta={"extractor": "x", "chunks": [1]})

    clear_extraction_stamp(doc)

    assert doc.held_for_split_at is None


# --- recovery sweeps -----------------------------------------------------------------------


def test_the_metadata_phase_sweep_skips_a_held_batch(db_session):
    batch = _batch(db_session)
    held = _doc(db_session, batch, held=True)
    _doc(db_session, batch)

    with patch("app.tasks.document_processing.dispatch_metadata_phase") as dispatch:
        assert pipeline_status.recover_unclaimed_ready_metadata_phases(db_session) == {
            "batches_dispatched": 0,
            "batch_ids": [],
        }
        dispatch.assert_not_called()

        # The safety net when the held document is deleted instead of decided.
        db_session.delete(db_session.get(Document, held.id))
        db_session.commit()
        result = pipeline_status.recover_unclaimed_ready_metadata_phases(db_session)

    assert result["batch_ids"] == [batch.id]


def test_the_stuck_dispatch_sweep_does_not_start_metadata_in_a_held_batch(db_session):
    batch = _batch(db_session)
    held = _doc(db_session, batch, held=True)
    sibling = _doc(db_session, batch)

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        result = pipeline_status.recover_stuck_pending_dispatches(db_session)
        assert result["doc_ids"] == []
        dispatch.assert_not_called()

        held_doc = db_session.get(Document, held.id)
        held_doc.held_for_split_at = None
        db_session.commit()
        result = pipeline_status.recover_stuck_pending_dispatches(db_session)

    assert sorted(result["doc_ids"]) == sorted([held.id, sibling.id])


# --- the gates of later documents ---------------------------------------------------------------


def test_a_held_batch_does_not_hold_the_relationships_gate_of_the_case(
    db_session, sample_case
):
    batch = _batch(db_session)
    earlier = _doc(db_session, batch, held=True, case_id=sample_case.id)
    db_session.query(DocumentPipelineStage).filter_by(
        document_id=earlier.id, stage="enrich"
    ).update({"status": "pending"})
    db_session.commit()
    later = _doc(db_session, _batch(db_session), case_id=sample_case.id)

    assert pipeline_status.relationships_gate_open(db_session, later.id)

    earlier.held_for_split_at = None
    db_session.commit()
    assert not pipeline_status.relationships_gate_open(db_session, later.id)


def test_the_case_brief_does_not_wait_for_a_held_batch(db_session, sample_case):
    done = _doc(db_session, _batch(db_session), case_id=sample_case.id)
    held = _doc(db_session, _batch(db_session), held=True, case_id=sample_case.id)
    db_session.query(DocumentPipelineStage).filter_by(
        document_id=done.id, stage="claims"
    ).update({"status": "completed"})
    db_session.commit()

    assert claim_case_brief_for_dispatch(sample_case.id, db_session) is True

    # Without the hold the unfinished CLAIMS of the other document blocks it again.
    db_session.execute(text("UPDATE cases SET brief_queued_at = NULL"))
    held_doc = db_session.get(Document, held.id)
    held_doc.held_for_split_at = None
    db_session.commit()
    assert claim_case_brief_for_dispatch(sample_case.id, db_session) is False


# --- processing queue ---------------------------------------------------------------------------


def test_the_queue_says_who_waits_for_the_split_decision(db_session):
    batch = _batch(db_session)
    held = _doc(db_session, batch, held=True)
    sibling = _doc(db_session, batch)
    other = _doc(db_session, _batch(db_session))
    held_batches = packet_hold.held_batch_ids(db_session)

    def note(doc):
        return _queue_item(
            {"type": "doc", "doc": doc, "stage": PipelineStage.METADATA}, held_batches
        ).note

    assert note(held) == "Awaiting your split decision"
    assert note(sibling) == "Waiting for a split decision in this email"
    assert note(other) is None


# --- Docling page markers ------------------------------------------------------------------------


def _marked(pages: list[str]) -> str:
    return "\n\n".join(f"--- PAGE {n} ---\n\n{t}" for n, t in enumerate(pages, 1))


def _docling(content, pages):
    return SimpleNamespace(
        content=content, meta={"extractor": "docling", "pages": pages}
    )


def test_page_texts_fall_back_to_the_markers_when_they_match_the_page_count():
    doc = _docling(_marked(["Brief", "Weiter", "Neuer Brief"]), 3)
    assert page_text.page_texts(doc) == {1: "Brief", 2: "Weiter", 3: "Neuer Brief"}


def test_marker_pages_without_text_are_dropped():
    doc = _docling(_marked(["Brief", "<!-- image -->", ""]), 3)
    assert page_text.page_texts(doc) == {1: "Brief"}


@pytest.mark.parametrize(
    "doc",
    [
        _docling(_marked(["a", "b"]), 3),  # markers do not cover meta.pages
        _docling(_marked(["a", "b", "c"]), 2),  # more markers than pages
        _docling("--- PAGE 2 ---\n\na\n\n--- PAGE 1 ---\n\nb", 2),  # out of order
        _docling(None, 2),
        SimpleNamespace(content=_marked(["a"]), meta={}),  # page count unknown
    ],
)
def test_irregular_markers_give_no_page_texts(doc):
    assert page_text.page_texts(doc) == {}


def test_chandra_chunks_win_over_markers():
    doc = SimpleNamespace(
        content=_marked(["x", "y"]),
        meta={
            "pages": 2,
            "chunks": [
                {"text": "eins", "meta": {"page": 1}},
                {"text": "zwei", "meta": {"page": 2, "failed": True}},
            ],
        },
    )
    assert page_text.page_texts(doc) == {1: "eins"}
