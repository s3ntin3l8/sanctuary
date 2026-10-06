"""/api/v1/home, /api/v1/cases, /api/v1/shell, /api/v1/search, /api/v1/worker-queue."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.timezone import now_utc
from app.main import app
from app.models.database import ActionItem, Case, Document, IngestBatch, Proceeding
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    PipelineState,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services import auth_service

pytestmark = pytest.mark.integration

client = TestClient(app)


def _admin(db):
    return auth_service.get_user_by_email(db, "admin@localhost")


# --- shell -------------------------------------------------------------------


def test_shell_returns_user_and_triage_count(db_session):
    admin = _admin(db_session)
    db_session.add(
        IngestBatch(
            owner_id=admin.id,
            source_type=IngestBatchSourceType.EMAIL,
            status=IngestBatchStatus.PENDING,
            case_id="_TRIAGE",
        )
    )
    db_session.commit()
    body = client.get("/api/v1/shell").json()
    assert body["user"]["email"] == "admin@localhost"
    assert body["user"]["role"] == "admin"
    assert body["triage_count"] == 1


def test_logout_clears_session(auth_enabled, db_session):
    auth_service.create_user(
        db_session,
        email="u@example.com",
        password="password123",  # pragma: allowlist secret
    )
    db_session.commit()
    c = TestClient(app, follow_redirects=False)
    c.post(
        "/api/v1/auth/login",
        json={
            "email": "u@example.com",
            "password": "password123",  # pragma: allowlist secret
        },
    )
    assert c.get("/api/v1/shell").status_code == 200
    assert c.post("/api/v1/auth/logout").status_code == 204
    assert c.get("/api/v1/shell").status_code == 401


# --- home --------------------------------------------------------------------


def test_home_is_caught_up_when_empty(db_session):
    with patch("app.services.home_service.get_signals", return_value=[]):
        body = client.get("/api/v1/home").json()
    assert body["caught_up"] is True
    assert body["today_items"] == []
    assert body["active_cases"] == []
    assert body["last_home_visit"] is None


def test_home_lists_deadlines_bundles_and_cases(db_session, sample_case):
    admin = _admin(db_session)
    db_session.add(
        Proceeding(
            case_id=sample_case.id,
            court_name="AG Hamburg",
            court_level=ProceedingCourtLevel.AG,
            status=ProceedingStatus.ACTIVE,
            subject_matter="Sorgerecht",
        )
    )
    db_session.add(
        ActionItem(
            case_id=sample_case.id,
            title="File counter-statement",
            action_type=ActionItemType.DEADLINE,
            status=ActionItemStatus.OPEN,
            due_date=now_utc() + timedelta(days=3),
        )
    )
    batch = IngestBatch(
        owner_id=admin.id,
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PENDING,
        case_id="_TRIAGE",
        subject="Klageerwiderung",
        sender_email="kanzlei@example.com",
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        Document(
            title="Klageerwiderung.pdf",
            owner_id=admin.id,
            case_id=sample_case.id,
            ingest_batch_id=batch.id,
            pipeline_state=PipelineState.RUNNING,
        )
    )
    db_session.commit()

    with patch("app.services.home_service.get_signals", return_value=[]):
        body = client.get("/api/v1/home").json()

    assert body["caught_up"] is False
    (item,) = body["today_items"]
    assert item["title"] == "File counter-statement"
    assert item["case_title"] == "Test Case"
    assert item["action_type"] == "deadline"

    (bundle,) = body["triage_bundles"]
    assert bundle["title"] == "Klageerwiderung"
    assert bundle["doc_count"] == 1
    assert bundle["case_id"] is None
    assert bundle["suggested_case_id"] == sample_case.id
    assert bundle["pipeline"] == {
        "total": 1,
        "running": 1,
        "pending": 0,
        "failed": 0,
        "completed": 0,
    }

    (card,) = body["active_cases"]
    assert card["id"] == sample_case.id
    assert card["status"] == "intake"
    assert card["status_label"] == "Intake"
    assert card["proceeding_name"] == "AG Hamburg"
    assert card["matter_type"] == "Sorgerecht"
    assert card["doc_count"] == 1
    assert card["open_action_count"] == 1
    assert card["next_action"]["title"] == "File counter-statement"


def test_review_all_persists_last_home_visit(db_session):
    assert client.get("/api/v1/home").json()["last_home_visit"] is None
    assert client.post("/api/v1/home/review-all").status_code == 204
    assert client.get("/api/v1/home").json()["last_home_visit"] is not None


# --- cases directory ---------------------------------------------------------


def test_cases_directory_counts_and_cards(db_session, sample_case):
    db_session.add(Case(id="CLOSED-1", title="Done", status=CaseStatus.CLOSED))
    db_session.commit()
    body = client.get("/api/v1/cases").json()
    assert body["total"] == 2
    assert body["counts_by_status"] == {"intake": 1, "closed": 1}
    assert {c["id"] for c in body["cases"]} == {"TEST-001", "CLOSED-1"}


def test_create_case_creates_active_proceeding(db_session):
    resp = client.post(
        "/api/v1/cases",
        json={
            "case_id": " NEW-1 ",
            "title": "Weber ./. Weber",
            "court_name": "AG Hamburg",
        },
    )
    assert resp.status_code == 201
    assert resp.json() == {"id": "NEW-1"}
    case = db_session.get(Case, "NEW-1")
    assert case.status == CaseStatus.INTAKE
    assert case.owner_id == _admin(db_session).id
    (proc,) = db_session.query(Proceeding).filter_by(case_id="NEW-1").all()
    assert proc.court_name == "AG Hamburg"
    assert proc.status == ProceedingStatus.ACTIVE

    dup = client.post(
        "/api/v1/cases",
        json={"case_id": "NEW-1", "title": "x", "court_name": "AG Hamburg"},
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "case_id_taken"


def test_create_case_rejects_blank_fields(db_session):
    resp = client.post(
        "/api/v1/cases", json={"case_id": "", "title": "x", "court_name": "AG"}
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


def test_confirm_close_closes_case_and_proceedings(db_session, sample_case):
    sample_case.pending_close = True
    db_session.add(
        Proceeding(
            case_id=sample_case.id,
            court_name="AG",
            court_level=ProceedingCourtLevel.AG,
            status=ProceedingStatus.ACTIVE,
        )
    )
    db_session.commit()
    assert (
        client.post(f"/api/v1/cases/{sample_case.id}/confirm-close").status_code == 204
    )
    db_session.expire_all()
    case = db_session.get(Case, sample_case.id)
    assert case.status == CaseStatus.CLOSED
    assert case.pending_close is False
    assert case.closed_at is not None
    (proc,) = db_session.query(Proceeding).filter_by(case_id=sample_case.id).all()
    assert proc.status == ProceedingStatus.CLOSED


def test_dismiss_close_keeps_status(db_session, sample_case):
    sample_case.pending_close = True
    sample_case.ai_brief = {"close_suggestion_rationale": "x", "summary": "keep"}
    db_session.commit()
    assert (
        client.post(f"/api/v1/cases/{sample_case.id}/dismiss-close").status_code == 204
    )
    db_session.expire_all()
    case = db_session.get(Case, sample_case.id)
    assert case.status == CaseStatus.INTAKE
    assert case.pending_close is False
    assert case.ai_brief == {"summary": "keep"}


def test_close_decisions_require_edit_access(auth_enabled, db_session, sample_case):
    other = auth_service.create_user(
        db_session,
        email="other@example.com",
        password="password123",  # pragma: allowlist secret
    )
    sample_case.owner_id = _admin(db_session).id
    db_session.commit()
    c = TestClient(app, follow_redirects=False)
    c.post(
        "/api/v1/auth/login",
        json={
            "email": other.email,
            "password": "password123",  # pragma: allowlist secret
        },
    )
    resp = c.post(f"/api/v1/cases/{sample_case.id}/confirm-close")
    assert resp.status_code in (403, 404)
    assert set(resp.json()) == {"detail", "code"}


# --- search ------------------------------------------------------------------


def test_search_returns_cases_documents_and_unique_contacts(db_session, sample_case):
    db_session.add_all(
        [
            Document(
                title="Weber letter 1", case_id=sample_case.id, sender="RA Müller"
            ),
            Document(
                title="Weber letter 2", case_id=sample_case.id, sender="RA Müller"
            ),
        ]
    )
    sample_case.title = "Weber ./. Weber"
    db_session.commit()
    body = client.get("/api/v1/search", params={"q": "Weber"}).json()
    assert [c["id"] for c in body["cases"]] == [sample_case.id]
    assert {d["title"] for d in body["documents"]} == {
        "Weber letter 1",
        "Weber letter 2",
    }
    assert body["contacts"] == [{"name": "RA Müller"}]
    assert body["total"] == 3


def test_search_requires_two_characters(db_session):
    resp = client.get("/api/v1/search", params={"q": "W"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


# --- worker queue ------------------------------------------------------------


def test_worker_queue_lists_failed_docs_and_retries_them(db_session):
    admin = _admin(db_session)
    doc = Document(
        title="Broken.pdf",
        owner_id=admin.id,
        case_id=None,
        pipeline_state=PipelineState.FAILED,
    )
    db_session.add(doc)
    db_session.commit()

    body = client.get("/api/v1/worker-queue").json()
    assert body["counts"] == {
        "executing": 0,
        "queued": 0,
        "failed": 1,
        "ai_inflight": 0,
    }
    assert body["failed"][0]["doc_id"] == doc.id
    assert body["failed"][0]["title"] == "Broken.pdf"

    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        resp = client.post("/api/v1/worker-queue/retry-failed")
    assert resp.status_code == 200
    assert {call.args[1] for call in dispatch.call_args_list} == {doc.id}
