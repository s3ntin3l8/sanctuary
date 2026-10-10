"""One misread department splits a bundle's Aktenzeichen; the user fixes it on the document."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, Document, IngestBatch, Proceeding, User
from app.models.enums import (
    CaseStatus,
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    PipelineStage,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services.ai_summary import _apply_proceeding_extraction
from app.services.ingestion.plausibility import az_conflict, az_department_variants
from app.services.ingestion.service import compute_review_reasons
from app.services.pipeline_status import mark_completed

pytestmark = pytest.mark.integration

client = TestClient(app)

GOOD = "3 F 2022/23"
BAD = "2 F 2022/23"


def _proc(db, case_id, az, **kw):
    proc = Proceeding(
        case_id=case_id,
        court_name="Amtsgericht Ingolstadt",
        court_level=ProceedingCourtLevel.AG,
        az_court=az,
        status=kw.pop("status", ProceedingStatus.ACTIVE),
        is_draft=kw.pop("is_draft", True),
    )
    db.add(proc)
    db.flush()
    return proc


@pytest.fixture
def bundle(db_session):
    """Three slices read ``3 F``, one reads ``2 F``; the ``2 F`` one seeded its own draft."""
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    db_session.add(
        Case(id="AZ-1", title="Sorgerecht", status=CaseStatus.INTAKE, owner_id=owner.id)
    )
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.SCAN,
        status=IngestBatchStatus.PROCESSING,
        received_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    db_session.add(batch)
    db_session.flush()
    good = _proc(db_session, "AZ-1", GOOD)
    stray = _proc(db_session, "AZ-1", BAD)
    batch.proceeding_id = stray.id
    docs = []
    for i, (az, proc) in enumerate(
        [(GOOD, good), (GOOD, good), (GOOD, good), (BAD, stray)]
    ):
        doc = Document(
            title=f"Slice {i}",
            owner_id=owner.id,
            case_id="AZ-1",
            ingest_batch_id=batch.id,
            role=DocumentRole.COVER_LETTER if i == 0 else DocumentRole.ENCLOSURE,
            originator_type=OriginatorType.COURT,
            az_court=az,
            proceeding_id=proc.id,
        )
        db_session.add(doc)
        docs.append(doc)
    db_session.commit()
    return docs, good, stray, batch


def test_department_variants():
    assert az_department_variants(GOOD, BAD)
    assert not az_department_variants(GOOD, GOOD)
    assert not az_department_variants(GOOD, "3 F 2022/24")  # other year
    assert not az_department_variants(GOOD, "3 C 2022/23")  # other register
    assert not az_department_variants(GOOD, "1 DR II 1030/26")  # not canonical
    assert not az_department_variants(GOOD, None)


def test_every_side_of_the_conflict_is_flagged(db_session, bundle):
    docs, *_ = bundle
    assert all("az_conflict" in compute_review_reasons(d) for d in docs)


def test_matching_aktenzeichen_are_not_flagged(db_session, bundle):
    docs, *_ = bundle
    docs[3].az_court = GOOD
    db_session.commit()
    assert not any("az_conflict" in compute_review_reasons(d) for d in docs)


def test_a_confirmed_side_ends_the_conflict_for_everyone(db_session, bundle):
    docs, *_ = bundle
    docs[3].extraction_confidence = {"az_court": "user_set"}
    db_session.commit()
    assert not any(az_conflict(d, db_session) for d in docs)


def test_editing_the_aktenzeichen_fixes_flags_proceeding_and_relationships(
    db_session, bundle
):
    docs, good, stray, batch = bundle
    bad_doc = docs[3]
    mark_completed(bad_doc.id, PipelineStage.RELATIONSHIPS, db_session)
    stray_id = stray.id

    with patch("app.services.triage_retry.dispatch_pipeline_retry") as dispatch:
        resp = client.put(
            f"/api/v1/documents/{bad_doc.id}/metadata",
            json={"az_court": "003 F 2022/23"},
        )
    assert resp.status_code == 200
    assert resp.json()["az_court"] == GOOD
    assert "az_conflict" not in resp.json()["review_reasons"]
    dispatch.assert_called_once()
    assert dispatch.call_args.args[2] == PipelineStage.RELATIONSHIPS

    db_session.expire_all()
    bad_doc = db_session.get(Document, bad_doc.id)
    assert bad_doc.proceeding_id == good.id
    assert bad_doc.extraction_confidence["az_court"] == "user_set"
    assert db_session.get(Proceeding, stray_id) is None
    assert db_session.get(IngestBatch, batch.id).proceeding_id == good.id
    for sibling in docs[:3]:
        assert "az_conflict" not in db_session.get(Document, sibling.id).review_reasons


def test_an_unchanged_aktenzeichen_does_not_rerun_relationships(db_session, bundle):
    docs, *_ = bundle
    mark_completed(docs[0].id, PipelineStage.RELATIONSHIPS, db_session)
    with patch("app.services.triage_retry.dispatch_pipeline_retry") as dispatch:
        client.put(f"/api/v1/documents/{docs[0].id}/metadata", json={"az_court": GOOD})
    dispatch.assert_not_called()


def test_a_pending_relationships_stage_is_left_to_run_on_its_own(db_session, bundle):
    docs, *_ = bundle
    with patch("app.services.triage_retry.dispatch_pipeline_retry") as dispatch:
        client.put(f"/api/v1/documents/{docs[3].id}/metadata", json={"az_court": GOOD})
    dispatch.assert_not_called()


def test_a_proceeding_with_other_records_survives_the_move(db_session, bundle):
    docs, good, stray, _ = bundle
    stray.is_draft = False  # confirmed by the user earlier
    db_session.commit()
    client.put(f"/api/v1/documents/{docs[3].id}/metadata", json={"az_court": GOOD})
    db_session.expire_all()
    assert db_session.get(Proceeding, stray.id) is not None


def test_a_new_aktenzeichen_gets_a_proceeding_and_reopens_a_closed_one(
    db_session, bundle
):
    docs, good, stray, _ = bundle
    closed = _proc(db_session, "AZ-1", "4 F 2022/23", status=ProceedingStatus.CLOSED)
    closed.ended_at = datetime(2026, 1, 1, tzinfo=UTC)
    db_session.commit()
    client.put(
        f"/api/v1/documents/{docs[3].id}/metadata", json={"az_court": "4 F 2022/23"}
    )
    db_session.expire_all()
    reopened = db_session.get(Proceeding, closed.id)
    assert reopened.status == ProceedingStatus.ACTIVE and reopened.ended_at is None
    assert db_session.get(Document, docs[3].id).proceeding_id == closed.id


def test_a_misread_department_does_not_close_the_right_proceeding(db_session, bundle):
    """The mis-OCR'd sibling runs last: the bundle stays on its proceeding."""
    docs, good, _stray, _ = bundle
    bad_doc = docs[3]
    bad_doc.proceeding_id = good.id
    bad_doc.az_court = BAD
    db_session.commit()

    skip = _apply_proceeding_extraction(
        bad_doc,
        {
            "is_court_document": True,
            "az_court": BAD,
            "court_name": "Amtsgericht Ingolstadt",
        },
        db_session,
    )
    db_session.commit()
    assert skip is None
    db_session.expire_all()
    assert db_session.get(Proceeding, good.id).status == ProceedingStatus.ACTIVE
    assert {db_session.get(Document, d.id).proceeding_id for d in docs} == {good.id}


def test_a_confirmed_aktenzeichen_still_opens_a_new_instance(db_session, bundle):
    docs, good, _stray, _ = bundle
    bad_doc = docs[3]
    bad_doc.proceeding_id = good.id
    bad_doc.extraction_confidence = {"az_court": "user_set"}
    db_session.commit()

    _apply_proceeding_extraction(
        bad_doc,
        {"is_court_document": True, "az_court": GOOD},  # the AI reads the old one
        db_session,
    )
    db_session.commit()
    db_session.expire_all()
    # The user's 2 F wins over the AI's 3 F and moves to its own proceeding.
    assert db_session.get(Document, bad_doc.id).proceeding_id != good.id
