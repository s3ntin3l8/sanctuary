"""AI-summary approval: undoable, and visible to triage without gating review state."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document, IngestBatch, User
from app.models.enums import IngestBatchSourceType, IngestBatchStatus, OriginatorType

pytestmark = pytest.mark.integration

client = TestClient(app)

SUMMARY = {
    "legal_significance": "Die Klage wird abgewiesen.",
    "required_action": "Frist notieren.",
    "financial_impact": None,
}


def _owner(db) -> int:
    return db.query(User).filter_by(email="admin@localhost").one().id


def _doc(db, case_id, **kw) -> Document:
    doc = Document(
        title="Beschluss",
        case_id=case_id,
        owner_id=_owner(db),
        originator_type=OriginatorType.COURT,
        **kw,
    )
    db.add(doc)
    db.commit()
    return doc


def test_approve_unapprove_roundtrip(db_session, sample_case):
    doc = _doc(db_session, sample_case.id, ai_summary=SUMMARY)

    r = client.post(f"/api/v1/documents/{doc.id}/summary", json={"action": "approve"})
    assert r.status_code == 200 and r.json()["approved_at"]

    r = client.post(f"/api/v1/documents/{doc.id}/summary", json={"action": "unapprove"})
    assert r.status_code == 200
    assert r.json()["approved_at"] is None
    assert r.json()["bullets"]  # the summary itself is kept


def _bundle(db, docs):
    batch = IngestBatch(
        owner_id=_owner(db),
        source_type=IngestBatchSourceType.EMAIL,
        subject="Klage",
        status=IngestBatchStatus.PROCESSING,
    )
    db.add(batch)
    db.flush()
    for d in docs:
        d.ingest_batch_id = batch.id
    db.commit()


def test_feed_flags_unapproved_summaries_without_touching_review_state(db_session):
    pending = _doc(
        db_session,
        "_TRIAGE",
        ai_summary=SUMMARY,
        review_reasons=["pending_confirmation"],
        needs_review=True,
    )
    approved = _doc(
        db_session,
        "_TRIAGE",
        ai_summary=SUMMARY,
        ai_summary_approved_at=datetime.now(UTC),
        review_reasons=["pending_confirmation"],
        needs_review=True,
    )
    errored = _doc(
        db_session,
        "_TRIAGE",
        ai_summary={"error": "timeout"},
        review_reasons=["pending_confirmation"],
        needs_review=True,
    )
    none = _doc(
        db_session,
        "_TRIAGE",
        review_reasons=["pending_confirmation"],
        needs_review=True,
    )
    _bundle(db_session, [pending, approved, errored, none])

    (bundle,) = client.get("/api/v1/triage").json()["bundles"]
    by_id = {d["id"]: d for d in bundle["documents"]}
    assert by_id[pending.id]["summary_pending"] is True
    assert by_id[approved.id]["summary_pending"] is False
    assert by_id[errored.id]["summary_pending"] is False  # an error isn't a summary
    assert by_id[none.id]["summary_pending"] is False
    assert bundle["summaries_pending"] == 1
    # It is not a review reason: file moves and the case "to review" filter key off those.
    assert bundle["open_review_reasons"] == []
    db_session.refresh(pending)
    assert pending.needs_review is True
    assert pending.review_reasons == ["pending_confirmation"]
