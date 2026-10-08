"""#57: re-detect newer docs that ran before an older doc arrived."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.database import Case, Document, DocumentPipelineStage
from app.models.enums import (
    CaseStatus,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
    SignificanceTier,
    StageStatus,
)
from app.services import pipeline_status
from app.services.intelligence import relationship_backfill as bf
from app.services.intelligence import relationship_detector as rd

pytestmark = pytest.mark.unit

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _doc(
    db,
    case,
    title,
    issued,
    *,
    tier=SignificanceTier.CRITICAL,
    rel_done=None,
    enrich_done=None,
    case_id=None,
):
    doc = Document(
        title=title,
        content="x",
        case_id=case_id or case.id,
        significance_tier=tier,
        originator_type=OriginatorType.COURT,
        issued_date=issued,
    )
    db.add(doc)
    db.flush()
    pipeline_status.initialize(doc, batched=True, db=db)
    for stage, done in (("enrich", enrich_done), ("relationships", rel_done)):
        row = (
            db.query(DocumentPipelineStage)
            .filter_by(document_id=doc.id, stage=stage)
            .one()
        )
        row.status = "completed" if done else "pending"
        row.completed_at = done
    db.commit()
    return doc


@pytest.fixture(autouse=True)
def _no_knn(monkeypatch):
    monkeypatch.setattr(bf, "nearest_document_ids", lambda *a, **k: [])


def test_only_docs_that_ran_before_the_late_doc_are_stale(db_session, sample_case):
    newer_ran_before = _doc(
        db_session, sample_case, "n1", datetime(2025, 6, 1), rel_done=T0, enrich_done=T0
    )
    newer_ran_after = _doc(
        db_session,
        sample_case,
        "n2",
        datetime(2025, 7, 1),
        rel_done=T0 + timedelta(days=2),
        enrich_done=T0,
    )
    late = _doc(
        db_session,
        sample_case,
        "late",
        datetime(2025, 1, 1),
        rel_done=T0 + timedelta(days=1),
        enrich_done=T0 + timedelta(days=1),
    )

    assert bf.stale_successors(db_session, late) == [newer_ran_before.id]
    assert newer_ran_after.id not in bf.stale_successors(db_session, late)


def test_older_docs_and_other_cases_and_low_tier_are_ignored(db_session, sample_case):
    older = _doc(db_session, sample_case, "older", datetime(2024, 1, 1), rel_done=T0)
    low = _doc(
        db_session,
        sample_case,
        "low",
        datetime(2025, 6, 1),
        rel_done=T0,
        tier=SignificanceTier.ADMINISTRATIVE,
    )
    db_session.add(
        Case(
            id="OTHER-1",
            title="Other",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    db_session.commit()
    elsewhere = _doc(
        db_session,
        sample_case,
        "other",
        datetime(2025, 6, 1),
        rel_done=T0,
        case_id="OTHER-1",
    )
    late = _doc(
        db_session,
        sample_case,
        "late",
        datetime(2025, 1, 1),
        rel_done=T0 + timedelta(days=1),
        enrich_done=T0 + timedelta(days=1),
    )

    result = bf.stale_successors(db_session, late)
    assert (
        older.id not in result and low.id not in result and elsewhere.id not in result
    )


def test_triage_doc_never_backfills(db_session, sample_case):
    _doc(
        db_session,
        sample_case,
        "n",
        datetime(2025, 6, 1),
        rel_done=T0,
        case_id="_TRIAGE",
    )
    late = _doc(
        db_session,
        sample_case,
        "late",
        datetime(2025, 1, 1),
        enrich_done=T0 + timedelta(days=1),
        case_id="_TRIAGE",
    )
    assert bf.stale_successors(db_session, late) == []


def test_cap_and_recency_window_and_knn(db_session, sample_case, monkeypatch):
    monkeypatch.setattr(bf, "MAX_BACKFILL", 2)
    # Threading pool = 3 - 1 = 2 slots.
    monkeypatch.setattr(rd, "MAX_CANDIDATES", 3)
    monkeypatch.setattr(rd, "_SEMANTIC_SLOTS", 1)
    newer = [
        _doc(
            db_session,
            sample_case,
            f"n{i}",
            datetime(2025, 6, 1 + i),
            rel_done=T0,
            enrich_done=T0,
        )
        for i in range(4)
    ]
    late = _doc(
        db_session,
        sample_case,
        "late",
        datetime(2025, 1, 1),
        enrich_done=T0 + timedelta(days=1),
    )

    # n0/n1 have <2 docs between them and `late`; n2/n3 have >=2, so only the
    # nearest-dated two qualify via recency, and the cap holds anyway.
    assert bf.stale_successors(db_session, late) == [newer[0].id, newer[1].id]

    # A semantic neighbour outside the recency window is still picked up.
    monkeypatch.setattr(bf, "MAX_BACKFILL", 10)
    monkeypatch.setattr(bf, "nearest_document_ids", lambda *a, **k: [newer[3].id])
    assert bf.stale_successors(db_session, late) == [
        newer[0].id,
        newer[1].id,
        newer[3].id,
    ]


def _run_dispatch(db_session, doc_id):
    with (
        patch("app.dependencies.get_db_session", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.tasks.detect_relationships.detect_relationships_task.delay"
        ) as delay,
    ):
        n = bf.dispatch_backfill(doc_id)
    return n, delay


def test_dispatch_resets_claims_and_runs_without_cascade(db_session, sample_case):
    newer = _doc(
        db_session, sample_case, "n", datetime(2025, 6, 1), rel_done=T0, enrich_done=T0
    )
    late = _doc(
        db_session,
        sample_case,
        "late",
        datetime(2025, 1, 1),
        enrich_done=T0 + timedelta(days=1),
    )

    n, delay = _run_dispatch(db_session, late.id)

    assert n == 1
    delay.assert_called_once_with(newer.id, backfill=False)
    row = (
        db_session.query(DocumentPipelineStage)
        .filter_by(document_id=newer.id, stage=PipelineStage.RELATIONSHIPS.value)
        .one()
    )
    assert row.status == StageStatus.RUNNING.value  # claimed for the dispatched task


def test_dispatch_skips_in_flight_stage(db_session, sample_case):
    newer = _doc(
        db_session, sample_case, "n", datetime(2025, 6, 1), rel_done=T0, enrich_done=T0
    )
    late = _doc(
        db_session,
        sample_case,
        "late",
        datetime(2025, 1, 1),
        enrich_done=T0 + timedelta(days=1),
    )
    db_session.query(DocumentPipelineStage).filter_by(
        document_id=newer.id, stage="relationships"
    ).update({"status": "running"})
    db_session.commit()

    n, delay = _run_dispatch(db_session, late.id)
    assert n == 0
    delay.assert_not_called()


@pytest.mark.parametrize(
    "backfill, skipped, expected",
    [
        (True, None, 1),
        (False, None, 0),  # re-runs never cascade
        (True, "no prior candidates in case", 0),
    ],
)
def test_task_triggers_backfill_only_after_a_real_run(
    db_session, sample_case, backfill, skipped, expected
):
    from app.tasks.detect_relationships import detect_relationships_task

    doc = _doc(db_session, sample_case, "d", datetime(2025, 1, 1), enrich_done=T0)
    doc.ai_summary_created_at = T0
    db_session.commit()

    with (
        patch("app.dependencies.get_db_session", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.relationship_detector.detect",
            return_value=skipped,
        ),
        patch(
            "app.services.intelligence.relationship_backfill.dispatch_backfill"
        ) as dispatch,
    ):
        detect_relationships_task.run(doc.id, backfill=backfill)

    assert dispatch.call_count == expected
