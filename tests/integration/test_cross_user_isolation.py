"""Cross-user isolation regressions (#146 remainder, #159, #163).

Each test sets up two users and asserts that one never sees or touches the
other's untriaged documents, claims or cases through the code path under test.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    Case,
    CaseShare,
    Claim,
    ClaimEvidence,
    ClaimMergeProposal,
    Document,
    DocumentRelationship,
    IngestBatch,
)
from app.models.enums import (
    CaseAccessLevel,
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
    ProposalConfidence,
    ProposalStatus,
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
)
from app.services import auth_service


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


def _case(db, case_id, owner_id, *, draft=False):
    case = Case(
        id=case_id,
        title=f"Case {case_id}",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=owner_id,
        is_draft=draft,
    )
    db.add(case)
    db.commit()
    return case


def _triage_doc(db, owner_id, title, *, batch=None):
    doc = Document(
        title=title,
        owner_id=owner_id,
        case_id="_TRIAGE",
        significance_tier=SignificanceTier.SIGNIFICANT,
        ingest_batch_id=batch.id if batch else None,
    )
    db.add(doc)
    db.commit()
    return doc


# --- relationship detection ---------------------------------------------------


@pytest.mark.integration
def test_relationship_candidates_in_triage_stay_within_owner(db_session, two_users):
    from app.services.intelligence.relationship_detector import _get_prior_docs

    a, b = two_users
    a_old = _triage_doc(db_session, a.id, "A earlier")
    _triage_doc(db_session, b.id, "B earlier")
    a_new = _triage_doc(db_session, a.id, "A later")

    with patch(
        "app.services.intelligence.relationship_detector.nearest_document_ids",
        return_value=[],
    ):
        candidates = _get_prior_docs(a_new, db_session)

    assert [c.id for c in candidates] == [a_old.id]


# --- HUD relationships ------------------------------------------------------------


@pytest.mark.integration
def test_hud_relationships_hide_other_users_documents(db_session, two_users):
    from app.services.hud_context import build_hud_context

    a, b = two_users
    mine = _triage_doc(db_session, a.id, "Mine")
    mine2 = _triage_doc(db_session, a.id, "Mine too")
    theirs = _triage_doc(db_session, b.id, "Secret title of B")
    for other in (mine2, theirs):
        db_session.add(
            DocumentRelationship(
                from_document_id=mine.id,
                to_document_id=other.id,
                relationship_type=RelationshipType.REFERENCES,
                confidence=RelationshipConfidence.AI_DETECTED,
            )
        )
    db_session.commit()

    ctx = build_hud_context(db_session, mine, viewer=a)

    titles = {r["title"] for r in ctx["relationships_out"]}
    assert titles == {"Mine too"}


# --- upload parent_id ----------------------------------------------------------------


@pytest.mark.integration
def test_upload_rejects_parent_owned_by_another_user(
    auth_enabled, db_session, two_users
):
    a, b = two_users
    theirs = _triage_doc(db_session, b.id, "B's doc")

    client = _client()
    _login(client, "a@example.com")
    resp = client.post(
        "/api/v1/upload",
        files={"files": ("x.pdf", b"%PDF-1.4 test", "application/pdf")},
        data={"parent_id": str(theirs.id)},
    )

    assert resp.status_code == 404
    db_session.expire_all()
    assert (
        db_session.query(Document).filter(Document.parent_id == theirs.id).count() == 0
    )


# --- reorder_documents ------------------------------------------------------------------


@pytest.mark.integration
def test_reorder_ignores_lead_doc_from_another_batch(db_session, two_users):
    from app.services.triage_subgroups import (
        ensure_sub_groups_initialized,
        reorder_documents,
    )

    a, b = two_users

    def _batch(owner_id):
        batch = IngestBatch(
            owner_id=owner_id,
            source_type=IngestBatchSourceType.EMAIL,
            status=IngestBatchStatus.PROCESSING,
        )
        db_session.add(batch)
        db_session.flush()
        return batch

    batch_a, batch_b = _batch(a.id), _batch(b.id)
    doc_a = _triage_doc(db_session, a.id, "A1", batch=batch_a)
    doc_b = _triage_doc(db_session, b.id, "B1", batch=batch_b)
    groups_a = {g.id for g in ensure_sub_groups_initialized(db_session, batch_a.id)}
    groups_b = {g.id for g in ensure_sub_groups_initialized(db_session, batch_b.id)}
    db_session.commit()
    assert groups_a.isdisjoint(groups_b)

    reorder_documents(db_session, batch_a.id, [doc_a.id], None, lead_doc_id=doc_b.id)
    db_session.commit()
    db_session.refresh(doc_a)

    assert doc_a.sub_group_id in groups_a


# --- Gmail OAuth ----------------------------------------------------------------------------


@pytest.mark.integration
def test_gmail_oauth_callback_requires_a_saved_state(
    auth_enabled, db_session, two_users
):
    client = _client()
    _login(client, "a@example.com")

    # No /start was ever called, so there is no saved state; a forged callback
    # without a state parameter used to compare None == None and pass.
    resp = client.get("/api/ingest/gmail/oauth/callback", params={"code": "forged"})

    assert resp.status_code == 400
    assert "state" in resp.json()["detail"].lower()


@pytest.mark.integration
def test_gmail_oauth_start_requires_login(auth_enabled, db_session):
    resp = _client().get("/api/ingest/gmail/oauth/start")

    assert resp.status_code in (401, 403, 302, 307)
    assert "accounts.google.com" not in resp.headers.get("location", "")


# --- 404 vs 403 on case assignment ---------------------------------------------------------------


@pytest.mark.integration
def test_assigning_to_an_unseen_case_is_404_but_view_only_case_is_403(
    auth_enabled, db_session, two_users
):
    a, b = two_users
    hidden = _case(db_session, "HIDDEN-B", b.id)
    shared = _case(db_session, "SHARED-B", b.id)
    db_session.add(
        CaseShare(case_id=shared.id, user_id=a.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.commit()
    batch = IngestBatch(
        owner_id=a.id,
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
    )
    db_session.add(batch)
    db_session.flush()
    _triage_doc(db_session, a.id, "mine", batch=batch)

    client = _client()
    _login(client, "a@example.com")

    def _assign(case_id):
        return client.post(
            "/api/v1/triage/batch/assign",
            json={"keys": [f"batch-{batch.id}"], "case_id": case_id},
        ).status_code

    assert _assign(hidden.id) == 404
    assert _assign("DOES-NOT-EXIST") == 404
    assert _assign(shared.id) == 403


# --- claim evidence across cases (#159) --------------------------------------------------------------


def _claim_across_cases(db, a, b):
    case_a = _case(db, "CLM-A", a.id)
    case_b = _case(db, "CLM-B", b.id)
    doc_a = Document(title="A evidence", owner_id=a.id, case_id=case_a.id)
    doc_b = Document(title="B evidence", owner_id=b.id, case_id=case_b.id)
    db.add_all([doc_a, doc_b])
    db.flush()
    claim = Claim(claim_text="Shared fact", status=ClaimStatus.ASSERTED)
    db.add(claim)
    db.flush()
    for doc in (doc_a, doc_b):
        db.add(
            ClaimEvidence(
                claim_id=claim.id, document_id=doc.id, role=ClaimEvidenceRole.ASSERTS
            )
        )
    db.commit()
    return case_a, case_b, doc_a, doc_b, claim


@pytest.mark.integration
def test_truth_map_hides_evidence_from_cases_the_viewer_cannot_see(
    db_session, two_users
):
    from app.services.claim_service import ClaimService

    a, b = two_users
    case_a, _, doc_a, _, _ = _claim_across_cases(db_session, a, b)

    view = ClaimService(db_session).get_truth_map(case_a.id, "all", viewer=a)

    rows = [r for g in view.groups for c in g.claims for r in c.evidence]
    assert [r.document.id for r in rows] == [doc_a.id]


@pytest.mark.integration
def test_truth_map_shows_evidence_from_cases_shared_with_the_viewer(
    db_session, two_users
):
    from app.services.claim_service import ClaimService

    a, b = two_users
    case_a, case_b, doc_a, doc_b, _ = _claim_across_cases(db_session, a, b)
    db_session.add(
        CaseShare(case_id=case_b.id, user_id=a.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.commit()

    view = ClaimService(db_session).get_truth_map(case_a.id, "all", viewer=a)

    rows = [r for g in view.groups for c in g.claims for r in c.evidence]
    assert {r.document.id for r in rows} == {doc_a.id, doc_b.id}


@pytest.mark.integration
def test_pending_merge_hides_claim_text_from_unseen_cases(db_session, two_users):
    from app.services.claim_service import ClaimService

    a, b = two_users
    case_a, case_b, doc_a, doc_b, claim = _claim_across_cases(db_session, a, b)
    other = Claim(claim_text="Secret claim text of B", status=ClaimStatus.ASSERTED)
    db_session.add(other)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=other.id, document_id=doc_b.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db_session.add(
        ClaimMergeProposal(
            new_claim_id=claim.id,
            existing_claim_id=other.id,
            confidence=ProposalConfidence.HIGH,
            status=ProposalStatus.PENDING,
        )
    )
    db_session.commit()

    view_a = ClaimService(db_session).get_truth_map(case_a.id, "all", viewer=a)
    view_b = ClaimService(db_session).get_truth_map(case_b.id, "all", viewer=b)

    assert view_a.pending_merges == []
    assert len(view_b.pending_merges) == 1


# --- drafts_pending is per-user (#163) -----------------------------------------------------------------


@pytest.mark.integration
def test_triage_stats_drafts_are_scoped_to_the_requesting_user(
    auth_enabled, db_session, two_users
):
    a, b = two_users
    draft_a = _case(db_session, "DRAFT-A", a.id, draft=True)
    draft_b = _case(db_session, "DRAFT-B", b.id, draft=True)
    doc_a = Document(title="a draft doc", owner_id=a.id, case_id=draft_a.id)
    doc_b = Document(title="b draft doc", owner_id=b.id, case_id=draft_b.id)
    db_session.add_all([doc_a, doc_b])
    db_session.commit()

    client = _client()
    _login(client, "a@example.com")
    stats = client.get("/api/v1/triage").json()["stats"]

    assert stats["drafts_pending"] == 1
    assert stats["first_draft_doc_id"] == doc_a.id


# --- data migration: drop already-created cross-owner edges ----------------------------------------------


@pytest.mark.integration
def test_migration_drops_only_cross_owner_triage_edges(db_session, two_users):
    import importlib.util
    from pathlib import Path

    from sqlalchemy import text

    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/e8a4c2f6b9d1_drop_cross_owner_triage_relationships.py"
    )
    spec = importlib.util.spec_from_file_location("drop_cross_owner_edges", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    a, b = two_users
    case_a = _case(db_session, "MIG-A", a.id)
    a1 = _triage_doc(db_session, a.id, "a1")
    a2 = _triage_doc(db_session, a.id, "a2")
    b1 = _triage_doc(db_session, b.id, "b1")
    in_case = Document(title="in case", owner_id=a.id, case_id=case_a.id)
    db_session.add(in_case)
    db_session.commit()

    def _edge(src, dst):
        db_session.add(
            DocumentRelationship(
                from_document_id=src.id,
                to_document_id=dst.id,
                relationship_type=RelationshipType.REFERENCES,
                confidence=RelationshipConfidence.AI_DETECTED,
            )
        )

    _edge(a1, b1)  # cross-owner, triage -> dropped
    _edge(a1, a2)  # same owner, triage -> kept
    _edge(a2, in_case)  # same owner, triage -> case -> kept
    db_session.commit()

    with patch(
        "alembic.op.execute", side_effect=lambda sql: db_session.execute(text(sql))
    ):
        migration.upgrade()
    db_session.commit()

    left = {
        (r.from_document_id, r.to_document_id)
        for r in db_session.query(DocumentRelationship).all()
    }
    assert left == {(a1.id, a2.id), (a2.id, in_case.id)}
