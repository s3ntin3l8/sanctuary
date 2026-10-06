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
    client.post("/api/v1/auth/login", json={"email": email, "password": password})


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
    body = client.get("/api/v1/triage").text
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
    resp = client.post(f"/api/v1/triage/bundles/{b_batch.id}/dismiss")
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
        "/api/v1/triage/batch/assign",
        json={"keys": [f"batch-{a_batch.id}"], "case_id": "OTHER-C"},
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


# --- owner-scoped email dedup (PR6) -----------------------------------------


def _build_email(
    message_id: str | None,
    subject: str,
    body: str = "Ein Schreiben ohne Anhang.",
) -> bytes:
    import email.message

    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes()


def test_same_message_id_ingested_by_two_users_yields_two_batches(
    db_session, two_users
):
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    a, b = two_users
    raw = _build_email("<shared-cc@example.com>", "CC'd to both of us")

    a_batch = ingest_raw_email(
        db_session, raw, source_type=IngestBatchSourceType.EMAIL, owner_id=a.id
    )
    b_batch = ingest_raw_email(
        db_session, raw, source_type=IngestBatchSourceType.EMAIL, owner_id=b.id
    )

    assert a_batch is not None
    assert b_batch is not None
    assert a_batch.id != b_batch.id
    assert a_batch.owner_id == a.id
    assert b_batch.owner_id == b.id


def test_orphan_reingest_does_not_touch_other_users_batch(db_session, two_users):
    """An orphaned (0-doc) batch is deleted-and-recreated on re-ingest of the
    same Message-ID — but only the *same user's* orphan. Before owner-scoping,
    get_by_message_id could return the other user's batch here and delete it."""
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    a, b = two_users
    raw = _build_email("<orphan-shared@example.com>", "No attachment, no body kept")

    a_batch = ingest_raw_email(
        db_session, raw, source_type=IngestBatchSourceType.EMAIL, owner_id=a.id
    )
    assert a_batch is not None
    a_batch_id = a_batch.id

    b_batch = ingest_raw_email(
        db_session, raw, source_type=IngestBatchSourceType.EMAIL, owner_id=b.id
    )
    assert b_batch is not None
    assert b_batch.id != a_batch_id

    db_session.expire_all()
    assert db_session.get(IngestBatch, a_batch_id) is not None, (
        "A's batch must still exist — B's ingest must never delete it"
    )


def test_subject_internal_id_auto_assign_scoped_to_owner(db_session, two_users):
    """A subject line referencing A's real case internal_id must not
    auto-assign B's email into A's case."""
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    a, b = two_users
    case = Case(
        id="8372-25",
        title="A's case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
    )
    db_session.add(case)
    db_session.commit()

    raw = _build_email("<b-cant-see-this@example.com>", "8372/25 Klage")
    b_batch = ingest_raw_email(
        db_session, raw, source_type=IngestBatchSourceType.EMAIL, owner_id=b.id
    )
    assert b_batch is not None
    assert b_batch.case_id in (None, "_TRIAGE")


def test_subject_internal_id_auto_assign_still_works_for_owner(db_session, two_users):
    """Sanity check for the fix above: the *owner* of the referenced case
    still gets auto-assigned — this isn't just failing closed for everyone."""
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    a, _b = two_users
    case = Case(
        id="9001-25",
        title="A's own case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
    )
    db_session.add(case)
    db_session.commit()

    raw = _build_email("<a-owns-this@example.com>", "9001/25 Klage")
    a_batch = ingest_raw_email(
        db_session, raw, source_type=IngestBatchSourceType.EMAIL, owner_id=a.id
    )
    assert a_batch is not None
    assert a_batch.case_id == "9001-25"


def test_admin_auto_assign_still_works_across_owners(db_session, two_users):
    """Admin ingesting an email is unrestricted — editable_case_ids returns
    None for admin, so auto-assign still resolves against any user's case."""
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    a, _b = two_users
    admin = auth_service.get_or_create_bootstrap_admin(db_session)
    db_session.commit()

    case = Case(
        id="7654-25",
        title="A's case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
    )
    db_session.add(case)
    db_session.commit()

    raw = _build_email("<admin-ingest@example.com>", "7654/25 Klage")
    admin_batch = ingest_raw_email(
        db_session, raw, source_type=IngestBatchSourceType.EMAIL, owner_id=admin.id
    )
    assert admin_batch is not None
    assert admin_batch.case_id == "7654-25"


def test_shared_az_court_still_matches_owners_own_proceeding(db_session, two_users):
    """A and B each have a Proceeding with the *same* Aktenzeichen (routine —
    both sides of one lawsuit share it). Ingesting an email for A referencing
    that az_court must match A's own proceeding/case, not shadow onto B's."""
    from app.models.database import Proceeding
    from app.models.enums import ProceedingCourtLevel, ProceedingStatus
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    a, b = two_users
    # Already in canonical form (normalize_az_court's output shape) so the
    # value stored on the Proceeding rows and the value re-derived from the
    # subject line via extract_az_court_from_subject match exactly.
    az_court = "3 F 426/25"

    case_a = Case(
        id="AZ-CASE-A",
        title="A's matter",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
    )
    case_b = Case(
        id="AZ-CASE-B",
        title="B's matter",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=b.id,
    )
    db_session.add_all(
        [case_b, case_a]
    )  # B first: an unfiltered query must not shadow A
    db_session.flush()
    proc_a = Proceeding(
        case_id=case_a.id,
        court_name="AG Testhausen",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
        az_court=az_court,
    )
    proc_b = Proceeding(
        case_id=case_b.id,
        court_name="AG Testhausen",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
        az_court=az_court,
    )
    db_session.add_all([proc_b, proc_a])  # same ordering rationale as above
    db_session.commit()

    raw = _build_email(
        "<a-az-court@example.com>", f"Schreiben - {az_court}", "kein Az-Anker im Body"
    )
    a_batch = ingest_raw_email(
        db_session, raw, source_type=IngestBatchSourceType.EMAIL, owner_id=a.id
    )
    assert a_batch is not None
    assert a_batch.case_id == case_a.id
    assert a_batch.proceeding_id == proc_a.id


# --- batch/triage-confirm defense-in-depth (PR6) ----------------------------


def test_batch_confirm_skips_bundle_with_inaccessible_suggested_case(
    auth_enabled, db_session, two_users
):
    """A owns the bundle but its case_id somehow already points at a case A
    can't edit (e.g. a stale row predating the ingest-time guards, or a
    since-revoked share) — batch_confirm must skip it, not silently confirm
    into a case A has no access to."""
    a, b = two_users
    case_b = Case(
        id="STALE-SUGGESTION",
        title="B's case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=b.id,
    )
    db_session.add(case_b)
    db_session.commit()

    a_batch, a_doc = _triage_batch(db_session, a.id, "StaleSuggestion")
    a_doc.case_id = case_b.id
    a_batch.case_id = case_b.id
    db_session.commit()

    client = _client()
    _login(client, "a@example.com")
    resp = client.post(
        "/api/v1/triage/batch/confirm", json={"keys": [f"batch-{a_batch.id}"]}
    )
    assert resp.status_code == 200
    assert resp.json()["skipped"] == 1

    db_session.expire_all()
    refreshed_batch = db_session.get(IngestBatch, a_batch.id)
    assert refreshed_batch.status != IngestBatchStatus.COMPLETED


def test_confirm_rejects_proceeding_id_from_a_different_case(
    auth_enabled, db_session, two_users
):
    """proceeding_id must belong to the case_id being confirmed into — a
    proceeding from an unrelated case must be rejected, not silently
    attached."""
    from app.models.database import Proceeding
    from app.models.enums import ProceedingCourtLevel, ProceedingStatus

    a, _b = two_users
    target_case = Case(
        id="TARGET-CASE",
        title="Target",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
    )
    other_case = Case(
        id="OTHER-CASE-FOR-A",
        title="Unrelated",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
    )
    db_session.add_all([target_case, other_case])
    db_session.flush()
    other_proc = Proceeding(
        case_id=other_case.id,
        court_name="AG Testhausen",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
    )
    db_session.add(other_proc)
    db_session.commit()

    a_batch, _a_doc = _triage_batch(db_session, a.id, "MismatchedProceeding")

    client = _client()
    _login(client, "a@example.com")
    resp = client.post(
        "/api/v1/triage/confirm",
        json={
            "batch_id": a_batch.id,
            "case_id": target_case.id,
            "proceeding_id": other_proc.id,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "proceeding_mismatch"


# --- triage page/OOB scoping (review-fix round) -----------------------------


def test_triage_page_proceedings_picker_excludes_other_users_courts(
    auth_enabled, db_session, two_users
):
    """The confirm-modal proceeding picker on GET /triage must not include
    another user's court name/Aktenzeichen — it renders every Proceeding
    row's data into <option> tags regardless of client-side JS filtering."""
    from app.models.database import Proceeding
    from app.models.enums import ProceedingCourtLevel, ProceedingStatus

    a, b = two_users
    case_a = Case(
        id="PICKER-A",
        title="A's case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
    )
    case_b = Case(
        id="PICKER-B",
        title="B's case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=b.id,
    )
    db_session.add_all([case_a, case_b])
    db_session.flush()
    db_session.add_all(
        [
            Proceeding(
                case_id=case_a.id,
                court_name="AG Aachen A-Court",
                court_level=ProceedingCourtLevel.AG,
                status=ProceedingStatus.ACTIVE,
            ),
            Proceeding(
                case_id=case_b.id,
                court_name="AG Berlin B-Secret-Court",
                court_level=ProceedingCourtLevel.AG,
                status=ProceedingStatus.ACTIVE,
            ),
        ]
    )
    db_session.commit()

    client = _client()
    _login(client, "a@example.com")
    body = client.get("/api/v1/triage").text
    assert "AG Aachen A-Court" in body
    assert "AG Berlin B-Secret-Court" not in body


def test_triage_count_after_delete_is_scoped_to_owner(
    auth_enabled, db_session, two_users
):
    """The shell badge reflects only the deleting user's own remaining triage."""
    a, b = two_users
    _triage_batch(db_session, a.id, "AKeep1")
    a_batch2, a_doc2 = _triage_batch(db_session, a.id, "ADelete")
    _triage_batch(db_session, b.id, "BOne")
    _triage_batch(db_session, b.id, "BTwo")
    _triage_batch(db_session, b.id, "BThree")

    client = _client()
    _login(client, "a@example.com")
    assert client.delete(f"/api/v1/documents/{a_doc2.id}").status_code == 204
    assert client.get("/api/v1/shell").json()["triage_count"] == 1


def test_confirm_draft_case_picker_uses_requester_identity_not_doc_owner(
    auth_enabled, db_session, two_users
):
    """The picker/badges shown after confirming a draft case must reflect the
    *confirming* user's own access, not the ingesting document's owner —
    these diverge whenever an EDITOR-shared user, not the owner, confirms."""
    a, b = two_users
    draft_case = Case(
        id="DRAFT-IDENTITY",
        title="Draft",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=a.id,
        is_draft=True,
    )
    db_session.add(draft_case)
    db_session.flush()
    db_session.add(
        CaseShare(
            case_id=draft_case.id, user_id=b.id, permission=CaseAccessLevel.EDITOR
        )
    )
    db_session.add(Document(title="A's doc", owner_id=a.id, case_id=draft_case.id))
    db_session.commit()

    client = _client()
    _login(client, "b@example.com")
    with patch("app.api.cases.CaseRepository.list_for_picker") as mock_picker:
        mock_picker.return_value = []
        resp = client.post(f"/cases/{draft_case.id}/confirm-draft")
        assert resp.status_code == 200
        mock_picker.assert_called_once_with(owner_id=b.id)


def test_admin_can_delete_other_users_untriaged_document(
    auth_enabled, db_session, two_users
):
    """An admin may delete another user's untriaged document (edit access)."""
    from app.models.enums import UserRole

    a, _b = two_users
    auth_service.create_user(
        db_session,
        email="admin-del@example.com",
        password="password123",  # pragma: allowlist secret
        role=UserRole.ADMIN,
    )
    db_session.commit()
    _a_batch, a_doc = _triage_batch(db_session, a.id, "AdminDeletesThis")

    client = _client()
    _login(client, "admin-del@example.com")
    doc_id = a_doc.id
    assert client.delete(f"/api/v1/documents/{doc_id}").status_code == 204
    db_session.expire_all()
    assert db_session.query(Document).filter(Document.id == doc_id).first() is None


def test_triage_feed_of_admin_does_not_show_other_users_sibling_docs(
    auth_enabled, db_session, two_users
):
    """A sibling in another user's batch never shows up in the admin's inbox."""
    from app.models.enums import UserRole

    a, _b = two_users
    auth_service.create_user(
        db_session,
        email="admin-sib@example.com",
        password="password123",  # pragma: allowlist secret
        role=UserRole.ADMIN,
    )
    db_session.commit()
    batch, doc1 = _triage_batch(db_session, a.id, "TwoDocBatch")
    doc2 = Document(
        title="TwoDocBatch doc 2",
        owner_id=a.id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        status=DocumentStatus.ACTIVE,
        needs_review=True,
    )
    db_session.add(doc2)
    db_session.commit()

    client = _client()
    _login(client, "admin-sib@example.com")
    assert client.delete(f"/api/v1/documents/{doc1.id}").status_code == 204
    feed = client.get("/api/v1/triage").json()
    assert all(d["id"] != doc2.id for b in feed["bundles"] for d in b["documents"])
