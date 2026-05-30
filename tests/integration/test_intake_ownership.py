"""Per-user intake ownership: triage inbox, shared-case invariant, gmail fan-out, scan folders."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    Case,
    CaseShare,
    Document,
    IngestBatch,
)
from app.models.enums import (
    CaseAccessLevel,
    CaseStatus,
    DocumentStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
)
from app.services import access_service, auth_service


def _client():
    return TestClient(app, follow_redirects=False)


def _login(client, email, password="password123"):
    client.post("/login", data={"email": email, "password": password})


@pytest.fixture
def two_users(db_session):
    a = auth_service.create_user(
        db_session, email="a@example.com", password="password123"
    )
    b = auth_service.create_user(
        db_session, email="b@example.com", password="password123"
    )
    db_session.commit()
    return a, b


def _triage_batch(db, owner_id, subject):
    batch = IngestBatch(
        owner_id=owner_id,
        source_type=IngestBatchSourceType.EMAIL,
        subject=subject,
        status=IngestBatchStatus.PROCESSING,
    )
    db.add(batch)
    db.flush()
    doc = Document(
        title=f"{subject} doc",
        owner_id=owner_id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        status=DocumentStatus.ACTIVE,
        needs_review=True,
    )
    db.add(doc)
    db.commit()
    return batch, doc


# --- per-user triage feed --------------------------------------------------


def test_triage_feed_is_per_user_service(db_session, two_users):
    from app.services.triage_bundles import get_triage_bundles

    a, b = two_users
    _triage_batch(db_session, a.id, "Alpha")
    _triage_batch(db_session, b.id, "Beta")

    a_subjects = {bn.subject for bn in get_triage_bundles(db_session, owner_id=a.id)}
    b_subjects = {bn.subject for bn in get_triage_bundles(db_session, owner_id=b.id)}
    assert "Alpha" in a_subjects and "Beta" not in a_subjects
    assert "Beta" in b_subjects and "Alpha" not in b_subjects


def test_triage_page_excludes_other_users(auth_enabled, db_session, two_users):
    a, b = two_users
    _triage_batch(db_session, a.id, "AlphaSubject")
    _triage_batch(db_session, b.id, "BetaSubject")

    client = _client()
    _login(client, "a@example.com")
    body = client.get("/triage").text
    assert "AlphaSubject doc" in body
    assert "BetaSubject doc" not in body


def test_sidebar_triage_count_is_per_user(db_session, two_users):
    from app.helpers import build_sidebar_counts

    a, b = two_users
    _triage_batch(db_session, a.id, "A1")
    _triage_batch(db_session, b.id, "B1")
    _triage_batch(db_session, b.id, "B2")

    assert build_sidebar_counts(db_session, owner_id=a.id)["triage_count"] == 1
    assert build_sidebar_counts(db_session, owner_id=b.id)["triage_count"] == 2


def test_triage_mutation_on_other_users_batch_404(auth_enabled, db_session, two_users):
    a, b = two_users
    b_batch, _ = _triage_batch(db_session, b.id, "BetaBatch")

    client = _client()
    _login(client, "a@example.com")
    # A tries to dismiss B's batch → guard 404
    resp = client.post(f"/triage/dismiss?batch_id={b_batch.id}")
    assert resp.status_code == 404


# --- cross-tenant write guard: can't assign into another user's case --------


def test_assign_to_other_users_case_forbidden(auth_enabled, db_session, two_users):
    a, b = two_users
    # B owns case C; A owns a triage bundle.
    case = Case(
        id="OTHER-C",
        title="B's case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=b.id,
    )
    db_session.add(case)
    db_session.commit()
    a_batch, _ = _triage_batch(db_session, a.id, "MyBundle")

    client = _client()
    _login(client, "a@example.com")
    resp = client.post(
        "/triage/batch/assign",
        data={"bundle_keys": [f"batch-{a_batch.id}"], "case_id": "OTHER-C"},
    )
    assert resp.status_code == 403
    # C must NOT have gained any document.
    db_session.expire_all()
    assert db_session.query(Document).filter(Document.case_id == "OTHER-C").count() == 0


# --- shared-case invariant: owner_id must NOT gate case-context docs --------


def test_shared_editor_sees_owner_ingested_doc_in_case(db_session, two_users):
    """A owns case C and shares editor to B. A document A ingested, assigned to C,
    must be visible to B in the case view — proving case-context queries are NOT
    filtered by Document.owner_id."""
    a, b = two_users
    case = Case(
        id="SHARED-C",
        title="Shared",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
    )
    db_session.add(case)
    db_session.add(
        CaseShare(case_id=case.id, user_id=b.id, permission=CaseAccessLevel.EDITOR)
    )
    db_session.add(Document(title="A's doc in C", owner_id=a.id, case_id=case.id))
    db_session.commit()

    # B can view the case (via share) ...
    assert access_service.can_view_case(db_session, b, case) is True
    # ... and the case's documents include A's doc (no owner filtering on cases).
    from app.services.case_service import CaseService

    data = CaseService(db_session).get_case_with_summary(case.id, b.id)
    titles = {d.title for d in data["documents"]}
    assert "A's doc in C" in titles


# --- gmail per-user fan-out -------------------------------------------------


def test_gmail_sync_fans_out_per_connected_user(db_session, two_users):
    from app.services import user_settings_service
    from app.tasks import gmail_sync

    a, b = two_users
    for u in (a, b):
        user_settings_service.set_gmail_credentials(
            db_session, u.id, credentials_json="{}", connected_at="2026-01-01"
        )
    db_session.commit()

    dispatched: list = []
    with (
        patch.object(gmail_sync, "user_ids_with_gmail", return_value=[a.id, b.id]),
        patch(
            "app.tasks.dispatch.dispatch_task",
            side_effect=lambda task, *args, **kw: dispatched.append((task, args)),
        ),
    ):
        gmail_sync.sync_gmail_incremental()

    assert {args[0] for _, args in dispatched} == {a.id, b.id}


# --- scan-folder per-user subfolders ---------------------------------------


def test_scan_folder_attributes_owner_by_subfolder(db_session, two_users):
    import app.config as cfg
    from app.services.ingestion import scan_folder

    a, _ = two_users
    admin = auth_service.get_or_create_bootstrap_admin(db_session)
    db_session.commit()

    incoming = cfg.SCAN_INCOMING_DIR
    (incoming / a.username).mkdir(parents=True, exist_ok=True)
    (incoming / a.username / "ua.pdf").write_bytes(b"%PDF-1.4 a")
    (incoming / "root.pdf").write_bytes(b"%PDF-1.4 root")

    captured: list = []

    def fake_ingest(_db, pdf_path, _batch_id, _source_hash, owner_id=None):
        captured.append((pdf_path.name, owner_id))
        return None  # treat as duplicate; we only assert owner attribution

    with (
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
        patch.object(scan_folder, "ingest_scanned_file", side_effect=fake_ingest),
    ):
        scan_folder.scan_and_ingest(db_session)

    # Two files processed: one owned by A (their subfolder), one by admin (root).
    owner_ids = {owner for _name, owner in captured}
    assert len(captured) == 2
    assert owner_ids == {a.id, admin.id}
