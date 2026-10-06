"""/api/v1/triage: feed, bundle actions, routing to cases, sub-groups."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    ActionItem,
    Case,
    Document,
    IngestBatch,
    Proceeding,
    User,
)
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    PipelineState,
    ProceedingCourtLevel,
    ProceedingStatus,
)

pytestmark = pytest.mark.integration

client = TestClient(app)


def _admin(db):
    return db.query(User).filter_by(email="admin@localhost").one()


def _batch(
    db,
    owner_id,
    subject="Klageerwiderung",
    docs=2,
    state=PipelineState.COMPLETED,
    case_id="_TRIAGE",
):
    batch = IngestBatch(
        owner_id=owner_id,
        source_type=IngestBatchSourceType.EMAIL,
        subject=subject,
        sender_email="kanzlei@example.com",
        status=IngestBatchStatus.PROCESSING,
        case_id=case_id if case_id != "_TRIAGE" else None,
    )
    db.add(batch)
    db.flush()
    created = []
    for i in range(docs):
        doc = Document(
            title=f"{subject} {i + 1}",
            owner_id=owner_id,
            case_id=case_id,
            ingest_batch_id=batch.id,
            pipeline_state=state,
            page_count=3,
            needs_review=True,
            review_reasons=["pending_confirmation"],
        )
        db.add(doc)
        created.append(doc)
    db.commit()
    return batch, created


# --- Feed --------------------------------------------------------------------


def test_feed_lists_bundles_with_stats_and_pickers(db_session, sample_case):
    admin = _admin(db_session)
    db_session.add(
        Proceeding(
            case_id=sample_case.id,
            court_name="AG Hamburg",
            court_level=ProceedingCourtLevel.AG,
            status=ProceedingStatus.ACTIVE,
        )
    )
    batch, docs = _batch(db_session, admin.id)
    db_session.add(
        ActionItem(
            case_id="_TRIAGE",
            source_document_id=docs[0].id,
            title="Frist",
            action_type=ActionItemType.DEADLINE,
            due_date=datetime.now(UTC) + timedelta(days=5),
        )
    )
    db_session.commit()

    body = client.get("/api/v1/triage").json()
    (bundle,) = body["bundles"]
    assert bundle["key"] == f"batch-{batch.id}"
    assert bundle["status"] == "needs_classification"
    assert bundle["pipeline"]["counts"]["completed"] == 2
    assert bundle["doc_count"] == 2
    assert bundle["total_pages"] == 6
    assert bundle["lead_doc_id"] in {d.id for d in docs}
    assert {d["id"] for d in bundle["documents"]} == {d.id for d in docs}
    assert len(bundle["sub_groups"]) >= 1
    assert body["stats"]["pending"] == 1
    assert body["stats"]["needs_classification"] == 1
    assert [c["id"] for c in body["cases"]] == [sample_case.id]
    assert body["proceedings"][0]["label"].startswith("AG Hamburg")
    assert body["filter_options"]["pipeline"]


def test_feed_filters_by_pipeline_state(db_session):
    admin = _admin(db_session)
    _batch(db_session, admin.id, "Ready")
    failed, _ = _batch(db_session, admin.id, "Broken", state=PipelineState.FAILED)
    body = client.get("/api/v1/triage", params={"pipeline_filter": ["failed"]}).json()
    assert [b["key"] for b in body["bundles"]] == [f"batch-{failed.id}"]
    assert body["bundles"][0]["status"] == "stuck"
    assert body["bundles"][0]["pipeline"]["failed_error"]


# --- Dismiss / delete / retry ------------------------------------------------


def test_dismiss_and_delete_bundle(db_session):
    admin = _admin(db_session)
    a, _ = _batch(db_session, admin.id, "A")
    b, _ = _batch(db_session, admin.id, "B")
    assert client.post(f"/api/v1/triage/bundles/{a.id}/dismiss").status_code == 204
    assert client.delete(f"/api/v1/triage/bundles/{b.id}").status_code == 204
    assert client.get("/api/v1/triage").json()["bundles"] == []
    assert client.post("/api/v1/triage/bundles/999999/dismiss").status_code == 404


def test_delete_refuses_in_flight_bundle(db_session):
    admin = _admin(db_session)
    batch, docs = _batch(db_session, admin.id, state=PipelineState.RUNNING)
    from app.models.database import DocumentPipelineStage
    from app.models.enums import PipelineStage, StageStatus

    db_session.add(
        DocumentPipelineStage(
            document_id=docs[0].id,
            stage=PipelineStage.ENRICH,
            status=StageStatus.RUNNING,
        )
    )
    db_session.commit()
    resp = client.delete(f"/api/v1/triage/bundles/{batch.id}")
    assert resp.status_code == 409
    assert resp.json()["code"] == "in_flight"


def test_retry_all_counts_bundles(db_session):
    admin = _admin(db_session)
    _batch(db_session, admin.id, "A", state=PipelineState.FAILED)
    _batch(db_session, admin.id, "B", state=PipelineState.FAILED)
    with patch("app.api.v1.triage.dispatch_batch_retry"):
        body = client.post("/api/v1/triage/retry-all").json()
    assert body["retried"] == 2


# --- Routing -----------------------------------------------------------------


def test_assign_keeps_bundle_in_triage_then_confirm_removes_it(db_session, sample_case):
    admin = _admin(db_session)
    batch, docs = _batch(db_session, admin.id)
    with patch("app.api.v1.triage.reset_and_reenrich"):
        resp = client.post(
            "/api/v1/triage/confirm",
            json={
                "batch_id": batch.id,
                "action": "assign_case",
                "case_id": sample_case.id,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["case"] == {
            "id": sample_case.id,
            "title": sample_case.title,
            "action": "assigned",
        }
        assert body["bundle"]["confirmed_case_id"] == sample_case.id
        assert body["bundle"]["status"] == "needs_review"

        resp = client.post(
            "/api/v1/triage/confirm",
            json={
                "batch_id": batch.id,
                "action": "confirm_bundle",
                "case_id": sample_case.id,
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["bundle"] is None
    assert resp.json()["next_doc_id"] is None
    db_session.expire_all()
    assert db_session.get(IngestBatch, batch.id).status == IngestBatchStatus.COMPLETED
    assert all(db_session.get(Document, d.id).case_id == sample_case.id for d in docs)


def test_confirm_creates_new_case(db_session):
    admin = _admin(db_session)
    batch, _ = _batch(db_session, admin.id)
    with patch("app.api.v1.triage.reset_and_reenrich"):
        resp = client.post(
            "/api/v1/triage/confirm",
            json={
                "batch_id": batch.id,
                "new_case_id": "NEW-7",
                "new_case_title": "Weber ./. Weber",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["case"]["action"] == "created"
    case = db_session.get(Case, "NEW-7")
    assert case is not None and case.owner_id == admin.id and case.is_draft is False


def test_confirm_validation(db_session, sample_case):
    admin = _admin(db_session)
    batch, docs = _batch(db_session, admin.id)
    assert (
        client.post(
            "/api/v1/triage/confirm", json={"case_id": sample_case.id}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/triage/confirm",
            json={
                "batch_id": batch.id,
                "doc_id": docs[0].id,
                "case_id": sample_case.id,
            },
        ).status_code
        == 422
    )
    missing = client.post("/api/v1/triage/confirm", json={"batch_id": batch.id})
    assert missing.status_code == 422
    unknown = client.post(
        "/api/v1/triage/confirm", json={"batch_id": batch.id, "case_id": "NOPE"}
    )
    assert unknown.status_code == 404


def test_loose_document_confirm_and_batch_assign(db_session, sample_case):
    admin = _admin(db_session)
    loose = Document(
        title="Loose",
        owner_id=admin.id,
        case_id="_TRIAGE",
        pipeline_state=PipelineState.COMPLETED,
    )
    db_session.add(loose)
    batch, _ = _batch(db_session, admin.id)
    db_session.commit()
    with patch("app.api.v1.triage.reset_and_reenrich"):
        resp = client.post(
            "/api/v1/triage/batch/assign",
            json={
                "keys": [f"loose-{loose.id}", f"batch-{batch.id}", "garbage"],
                "case_id": sample_case.id,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["confirmed"] == 2
    assert body["skipped"] == 1
    assert {b["key"] for b in body["bundles"]} == {
        f"loose-{loose.id}",
        f"batch-{batch.id}",
    }
    assert body["removed_keys"] == []
    db_session.expire_all()
    assert db_session.get(Document, loose.id).case_id == sample_case.id


def test_batch_confirm_uses_each_bundles_suggestion(db_session, sample_case):
    admin = _admin(db_session)
    suggested, _ = _batch(db_session, admin.id, "Suggested", case_id=sample_case.id)
    suggested.case_id = None
    nothing, _ = _batch(db_session, admin.id, "NoSuggestion")
    db_session.commit()
    with patch("app.api.v1.triage.reset_and_reenrich"):
        body = client.post(
            "/api/v1/triage/batch/confirm",
            json={"keys": [f"batch-{suggested.id}", f"batch-{nothing.id}"]},
        ).json()
    assert body["confirmed"] == 1
    assert body["skipped"] == 1
    assert body["removed_keys"] == [f"batch-{suggested.id}"]


# --- Title and sub-groups ----------------------------------------------------


def test_title_and_sub_group_lifecycle(db_session):
    admin = _admin(db_session)
    batch, docs = _batch(db_session, admin.id, docs=3)
    assert (
        client.put(
            f"/api/v1/triage/documents/{docs[0].id}/title", json={"title": " Renamed "}
        ).status_code
        == 204
    )
    db_session.expire_all()
    assert db_session.get(Document, docs[0].id).title == "Renamed"

    created = client.post(f"/api/v1/triage/bundles/{batch.id}/groups")
    assert created.status_code == 200, created.text
    groups = created.json()["sub_groups"]
    assert created.json()["has_manual_groups"] is True
    new_group = next(g for g in groups if not g["doc_ids"])
    sgid = new_group["sub_group_id"]

    renamed = client.put(
        f"/api/v1/triage/bundles/{batch.id}/groups/{sgid}", json={"label": "Anlagen"}
    )
    assert renamed.status_code == 200, renamed.text
    assert any(g["label"] == "Anlagen" for g in renamed.json()["sub_groups"])

    ordered = client.put(
        f"/api/v1/triage/bundles/{batch.id}/groups/{sgid}/order",
        json={"doc_ids": [docs[2].id, docs[1].id]},
    )
    moved = next(g for g in ordered.json()["sub_groups"] if g["sub_group_id"] == sgid)
    assert moved["doc_ids"] == [docs[2].id, docs[1].id]

    cover = client.put(
        f"/api/v1/triage/bundles/{batch.id}/cover", json={"doc_id": docs[1].id}
    )
    assert cover.status_code == 200
    assert (
        next(d for d in cover.json()["documents"] if d["id"] == docs[1].id)["role"]
        == "cover_letter"
    )

    assert (
        client.delete(f"/api/v1/triage/bundles/{batch.id}/groups/{sgid}").status_code
        == 200
    )
    reset = client.post(f"/api/v1/triage/bundles/{batch.id}/groups/reset")
    assert reset.json()["has_manual_groups"] is False


def test_sub_group_errors_are_422(db_session):
    admin = _admin(db_session)
    batch, docs = _batch(db_session, admin.id)
    other, other_docs = _batch(db_session, admin.id, "Other")
    resp = client.put(
        f"/api/v1/triage/bundles/{batch.id}/cover", json={"doc_id": other_docs[0].id}
    )
    assert resp.status_code == 422
    assert (
        client.put(
            f"/api/v1/triage/bundles/{batch.id}/groups/999999", json={"label": "x"}
        ).status_code
        == 422
    )


def test_moving_last_doc_off_a_draft_deletes_the_draft(db_session):
    admin = _admin(db_session)
    real = Case(
        id="REAL-CLEAN-1",
        title="Real Case",
        status=CaseStatus.INTAKE,
        owner_id=admin.id,
    )
    draft = Case(
        id="DRAFT-ORPH-1",
        title="Draft",
        status=CaseStatus.INTAKE,
        is_draft=True,
        owner_id=admin.id,
    )
    keeper = Case(
        id="DRAFT-KEEP-1",
        title="Keeper",
        status=CaseStatus.INTAKE,
        is_draft=True,
        owner_id=admin.id,
    )
    db_session.add_all([real, draft, keeper])
    db_session.flush()
    mover = Document(
        title="Mover", owner_id=admin.id, case_id="DRAFT-ORPH-1", needs_review=True
    )
    stays = Document(
        title="Stays", owner_id=admin.id, case_id="DRAFT-KEEP-1", needs_review=True
    )
    db_session.add_all([mover, stays])
    db_session.commit()

    with patch("app.api.v1.triage.reset_and_reenrich"):
        resp = client.post(
            "/api/v1/triage/confirm",
            json={
                "doc_id": mover.id,
                "action": "assign_case",
                "case_id": "REAL-CLEAN-1",
            },
        )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(Case, "DRAFT-ORPH-1") is None
    assert db_session.get(Case, "DRAFT-KEEP-1") is not None
    assert db_session.get(Document, mover.id).case_id == "REAL-CLEAN-1"


def test_whitespace_title_is_rejected(db_session):
    admin = _admin(db_session)
    _, docs = _batch(db_session, admin.id, docs=1)
    resp = client.put(
        f"/api/v1/triage/documents/{docs[0].id}/title", json={"title": "   "}
    )
    assert resp.status_code == 422
    db_session.expire_all()
    assert db_session.get(Document, docs[0].id).title == "Klageerwiderung 1"


def test_mutations_find_bundles_beyond_the_default_feed_window(db_session):
    """Post-mutation lookups must not depend on the service's 50-bundle default."""
    admin = _admin(db_session)
    old_batch, _ = _batch(db_session, admin.id, subject="Oldest", docs=1)
    for i in range(50):
        _batch(db_session, admin.id, subject=f"Newer {i}", docs=1)
    with patch("app.api.v1.triage.dispatch_batch_retry"):
        resp = client.post(
            f"/api/v1/triage/bundles/{old_batch.id}/retry", json={"full": False}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject"] == "Oldest"
    grouped = client.post(f"/api/v1/triage/bundles/{old_batch.id}/groups")
    assert grouped.status_code == 200, grouped.text
    assert grouped.json()["key"] == f"batch-{old_batch.id}"
