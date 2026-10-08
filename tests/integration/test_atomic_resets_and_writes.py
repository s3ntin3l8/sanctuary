"""Atomic stage resets (#157) and race-safe entity/relationship writes (#142)."""

import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.models.database import (
    Case,
    Document,
    DocumentRelationship,
    Entity,
    IngestBatch,
)
from app.models.enums import (
    CaseStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
    PipelineStage,
    RelationshipConfidence,
    RelationshipType,
)
from app.services import auth_service
from app.services.pipeline_status import (
    initialize,
    reset_all_stages,
    reset_failed_stages_only,
    reset_stage,
)

_PASSWORD = "password123"  # pragma: allowlist secret


def _set(db, doc_id, stage, status):
    db.execute(
        text(
            "UPDATE document_pipeline_stages SET status = :s "
            "WHERE document_id = :d AND stage = :st"
        ),
        {"s": status, "d": doc_id, "st": stage},
    )
    db.commit()


def _status(db, doc_id, stage) -> str:
    return db.execute(
        text(
            "SELECT status FROM document_pipeline_stages "
            "WHERE document_id = :d AND stage = :st"
        ),
        {"d": doc_id, "st": stage},
    ).scalar_one()


@pytest.fixture
def doc(db_session, sample_case):
    d = Document(title="D", content="x", case_id=sample_case.id)
    db_session.add(d)
    db_session.flush()
    initialize(d, batched=False, db=db_session)
    db_session.commit()
    return d


# --- guarded resets ------------------------------------------------------------


@pytest.mark.integration
def test_reset_stage_refuses_an_in_flight_stage(db_session, doc):
    _set(db_session, doc.id, "enrich", "running")
    _set(db_session, doc.id, "claims", "completed")

    assert reset_stage(doc.id, PipelineStage.ENRICH, db_session) is False

    assert _status(db_session, doc.id, "enrich") == "running"  # not clobbered
    assert _status(db_session, doc.id, "claims") == "completed"  # downstream untouched


@pytest.mark.integration
def test_reset_stage_resets_a_settled_stage_and_its_downstream(db_session, doc):
    _set(db_session, doc.id, "enrich", "completed")
    _set(db_session, doc.id, "claims", "completed")

    assert reset_stage(doc.id, PipelineStage.ENRICH, db_session) is True

    assert _status(db_session, doc.id, "enrich") == "pending"
    assert _status(db_session, doc.id, "claims") == "pending"


@pytest.mark.integration
def test_reset_stage_force_resets_the_tasks_own_running_stage(db_session, doc):
    _set(db_session, doc.id, "enrich", "running")

    assert reset_stage(doc.id, PipelineStage.ENRICH, db_session, force=True) is True

    assert _status(db_session, doc.id, "enrich") == "pending"


@pytest.mark.integration
def test_force_only_lifts_the_guard_for_the_target_stage(db_session, doc):
    _set(db_session, doc.id, "enrich", "running")
    _set(db_session, doc.id, "claims", "running")

    assert reset_stage(doc.id, PipelineStage.ENRICH, db_session, force=True) is True

    assert _status(db_session, doc.id, "enrich") == "pending"
    assert _status(db_session, doc.id, "claims") == "running"  # not clobbered


@pytest.mark.integration
def test_reset_stage_leaves_a_running_downstream_stage_alone(db_session, doc):
    _set(db_session, doc.id, "enrich", "completed")
    _set(db_session, doc.id, "claims", "running")

    assert reset_stage(doc.id, PipelineStage.ENRICH, db_session) is True

    assert _status(db_session, doc.id, "enrich") == "pending"
    assert _status(db_session, doc.id, "claims") == "running"


@pytest.mark.integration
def test_reset_all_stages_is_all_or_nothing(db_session, doc):
    _set(db_session, doc.id, "extract", "completed")
    _set(db_session, doc.id, "metadata", "completed")
    _set(db_session, doc.id, "enrich", "running")

    assert reset_all_stages(doc.id, db_session) == ["enrich"]

    assert _status(db_session, doc.id, "extract") == "completed"
    assert _status(db_session, doc.id, "metadata") == "completed"
    assert _status(db_session, doc.id, "enrich") == "running"

    _set(db_session, doc.id, "enrich", "completed")
    assert reset_all_stages(doc.id, db_session) == []
    assert _status(db_session, doc.id, "extract") == "pending"


@pytest.mark.integration
def test_reset_failed_only_skips_a_stage_reclaimed_since_it_was_read(db_session, doc):
    """The failed row is re-claimed by another dispatcher after our read but
    before our write: the write must not put it back to PENDING."""
    _set(db_session, doc.id, "metadata", "failed")
    real_execute = db_session.execute
    state = {"raced": False}

    def racing_execute(stmt, *a, **kw):
        result = real_execute(stmt, *a, **kw)
        if not state["raced"] and "SELECT stage, status" in str(stmt):
            state["raced"] = True
            real_execute(
                text(
                    "UPDATE document_pipeline_stages SET status = 'running' "
                    "WHERE document_id = :d AND stage = 'metadata'"
                ),
                {"d": doc.id},
            )
        return result

    with patch.object(db_session, "execute", side_effect=racing_execute):
        reset_failed_stages_only(doc.id, db_session)

    assert _status(db_session, doc.id, "metadata") == "running"


@pytest.mark.integration
def test_reset_and_reenrich_does_not_dispatch_over_an_in_flight_enrich(db_session, doc):
    from app.services.triage_confirmation import reset_and_reenrich

    _set(db_session, doc.id, "metadata", "completed")
    _set(db_session, doc.id, "enrich", "running")
    db_session.refresh(doc)

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        reset_and_reenrich(db_session, [doc])

    dispatch.assert_not_called()
    assert _status(db_session, doc.id, "enrich") == "running"


# --- slicing retry -------------------------------------------------------------------


@pytest.mark.integration
def test_slicing_retry_dispatches_once_for_a_double_post(auth_enabled, db_session):
    user = auth_service.create_user(
        db_session,
        email="s@example.com",
        password=_PASSWORD,
    )
    batch = IngestBatch(
        owner_id=user.id,
        source_type=IngestBatchSourceType.MANUAL,
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": {"status": "failed", "error": "boom"}},
    )
    db_session.add(batch)
    db_session.commit()

    client = TestClient(app, follow_redirects=False)
    client.post(
        "/api/v1/auth/login",
        json={"email": "s@example.com", "password": _PASSWORD},
    )
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        first = client.post(f"/api/v1/slicing/{batch.id}/retry")
        second = client.post(f"/api/v1/slicing/{batch.id}/retry")

    assert first.status_code == 200
    assert second.status_code == 409
    assert dispatch.call_count == 1


# --- relationship edges ----------------------------------------------------------------


@pytest.mark.integration
def test_insert_edge_if_absent_is_idempotent_and_never_raises(db_session, sample_case):
    from app.repositories.document_relationship import insert_edge_if_absent

    a = Document(title="A", case_id=sample_case.id)
    b = Document(title="B", case_id=sample_case.id)
    db_session.add_all([a, b])
    db_session.commit()

    kwargs = {
        "from_document_id": a.id,
        "to_document_id": b.id,
        "relationship_type": RelationshipType.REFERENCES,
        "confidence": RelationshipConfidence.AI_DETECTED,
    }
    assert insert_edge_if_absent(db_session, **kwargs) is True
    assert insert_edge_if_absent(db_session, **kwargs) is False
    db_session.commit()  # a duplicate must not poison the transaction

    assert db_session.query(DocumentRelationship).count() == 1


# --- entities ---------------------------------------------------------------------------------


def _case_and_doc(factory, case_id):
    db = factory()
    db.add(
        Case(
            id=case_id,
            title=case_id,
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    doc = Document(title="D", case_id=case_id)
    db.add(doc)
    db.commit()
    doc_id = doc.id
    db.close()
    return doc_id


@pytest.mark.integration
def test_save_entities_waits_for_a_concurrent_writer_on_the_same_case(
    db_session_factory,
):
    from app.core.db_locks import ENTITIES_NS, advisory_xact_lock
    from app.services.intelligence.entity_extractor import _save_entities

    doc_id = _case_and_doc(db_session_factory, "ENT-LOCK")
    payload = {"entities": [{"type": "COURT", "name": "Amtsgericht X"}]}

    holder = db_session_factory()
    advisory_xact_lock(holder, ENTITIES_NS, "ENT-LOCK")  # another writer is mid-save

    finished = threading.Event()

    def _writer():
        s = db_session_factory()
        try:
            _save_entities(s.get(Document, doc_id), payload, s)
        finally:
            s.close()
            finished.set()

    t = threading.Thread(target=_writer)
    t.start()
    time.sleep(1.0)
    assert not finished.is_set(), "writer must block while the case lock is held"

    holder.commit()
    t.join(timeout=30)
    assert finished.is_set()
    holder.close()


@pytest.mark.integration
def test_concurrent_entity_saves_do_not_duplicate(db_session_factory):
    from app.services.intelligence.entity_extractor import _save_entities

    doc_id = _case_and_doc(db_session_factory, "ENT-RACE")
    payload = {"entities": [{"type": "COURT", "name": "Amtsgericht Y"}]}
    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def _writer():
        s = db_session_factory()
        try:
            barrier.wait(timeout=10)
            _save_entities(s.get(Document, doc_id), payload, s)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=_writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    check = db_session_factory()
    try:
        assert not errors
        assert check.query(Entity).filter(Entity.case_id == "ENT-RACE").count() == 1
    finally:
        check.close()


@pytest.mark.integration
def test_batch_analysis_retry_409s_when_every_failed_sibling_was_reclaimed(
    auth_enabled, db_session
):
    """The requested doc's reset is race-safe; so must the sibling loop be."""
    user = auth_service.create_user(
        db_session, email="b@example.com", password=_PASSWORD
    )
    case = Case(
        id="SIB-1",
        title="t",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=user.id,
    )
    batch = IngestBatch(
        owner_id=user.id,
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
    )
    db_session.add_all([case, batch])
    db_session.flush()
    doc_a = Document(
        title="A", case_id=case.id, owner_id=user.id, ingest_batch_id=batch.id
    )
    doc_b = Document(
        title="B", case_id=case.id, owner_id=user.id, ingest_batch_id=batch.id
    )
    db_session.add_all([doc_a, doc_b])
    db_session.flush()
    for d in (doc_a, doc_b):
        initialize(d, batched=True, db=db_session)
    db_session.commit()
    for d in (doc_a, doc_b):
        _set(db_session, d.id, "extract", "completed")
        _set(db_session, d.id, "metadata", "completed")
        _set(db_session, d.id, "batch_analysis", "failed")

    client = TestClient(app, follow_redirects=False)
    client.post(
        "/api/v1/auth/login", json={"email": "b@example.com", "password": _PASSWORD}
    )
    url = f"/api/v1/documents/{doc_a.id}/pipeline/batch_analysis/retry"

    with (
        patch("app.api.v1.documents.reset_stage", return_value=False),
        patch("app.api.v1.documents.dispatch_pipeline_retry") as dispatch,
    ):
        lost_race = client.post(url)
    assert lost_race.status_code == 409
    assert lost_race.json()["code"] == "in_flight"
    dispatch.assert_not_called()

    with patch("app.api.v1.documents.dispatch_pipeline_retry") as dispatch:
        ok = client.post(url)
    assert ok.status_code == 200
    dispatch.assert_called_once()
