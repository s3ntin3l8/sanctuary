"""Cross-user authorization matrix for PR5's API-wide access-guard sweep.

Follows the two_users / auth_enabled / real-login pattern established in
tests/integration/test_isolation.py. Covers documents, cases, costs,
proceedings, chat, worker-queue and claims — one guard per resource type is
mutation-tested in test_route_authorization_coverage.py; this file verifies
the guards actually produce the right *behavior* (404 for a non-owner, 200
for the owner, edit vs. view for a shared user).
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, CaseShare, Document, LegalCost, Proceeding
from app.models.enums import (
    CaseAccessLevel,
    CaseStatus,
    CostCategory,
    CostStatus,
    Jurisdiction,
    PipelineState,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services import auth_service


def _make_case(db, case_id, owner_id):
    case = Case(
        id=case_id,
        title=f"Case {case_id}",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=owner_id,
    )
    db.add(case)
    db.commit()
    return case


def _login(email: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"email": email, "password": "password123"})
    return client


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


# --- documents.py ------------------------------------------------------------


def test_delete_document_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    doc = Document(title="A's doc", owner_id=a.id, case_id=None)
    db_session.add(doc)
    db_session.commit()

    client = _login("b@example.com")
    resp = client.delete(f"/document/{doc.id}")
    assert resp.status_code == 404


def test_delete_document_ok_for_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    doc = Document(title="A's doc", owner_id=a.id, case_id=None)
    db_session.add(doc)
    db_session.commit()

    client = _login("a@example.com")
    resp = client.delete(f"/document/{doc.id}")
    assert resp.status_code == 200


def test_document_original_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    doc = Document(title="A's doc", owner_id=a.id, case_id=None, file_path=None)
    db_session.add(doc)
    db_session.commit()

    client = _login("b@example.com")
    resp = client.get(f"/document/{doc.id}/original")
    assert resp.status_code == 404


# --- cases.py ----------------------------------------------------------------


def test_update_case_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)

    client = _login("b@example.com")
    resp = client.patch("/cases/PR5-A", data={"title": "Hijacked"})
    assert resp.status_code == 404


def test_update_case_ok_for_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)

    client = _login("a@example.com")
    resp = client.patch("/cases/PR5-A", data={"title": "Renamed"})
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(Case, "PR5-A").title == "Renamed"


def test_update_case_forbidden_for_viewer_share(auth_enabled, db_session, two_users):
    """A VIEWER share can read the case but not mutate it."""
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)
    db_session.add(
        CaseShare(case_id="PR5-A", user_id=b.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.commit()

    client = _login("b@example.com")
    assert client.get("/cases/PR5-A").status_code == 200
    resp = client.patch("/cases/PR5-A", data={"title": "Hijacked"})
    assert resp.status_code == 404


def test_update_case_ok_for_editor_share(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)
    db_session.add(
        CaseShare(case_id="PR5-A", user_id=b.id, permission=CaseAccessLevel.EDITOR)
    )
    db_session.commit()

    client = _login("b@example.com")
    resp = client.patch("/cases/PR5-A", data={"title": "Edited by editor"})
    assert resp.status_code == 200


def test_purge_case_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)

    client = _login("b@example.com")
    resp = client.request(
        "DELETE", "/cases/PR5-A/purge", json={"confirm": "purge PR5-A"}
    )
    assert resp.status_code == 404


def test_create_case_owner_can_immediately_edit_it(auth_enabled, db_session, two_users):
    """Regression guard: create_case must stamp owner_id, or the creator would
    be locked out of their own case the moment PR5's edit guard is wired up."""
    a, _b = two_users
    client = _login("a@example.com")
    resp = client.post(
        "/cases",
        data={"case_id": "PR5-NEW", "title": "New Case", "court_name": "AG Berlin"},
    )
    assert resp.status_code in (200, 303)
    db_session.expire_all()
    case = db_session.get(Case, "PR5-NEW")
    assert case is not None
    assert case.owner_id == a.id

    edit_resp = client.patch("/cases/PR5-NEW", data={"title": "Renamed by owner"})
    assert edit_resp.status_code == 200


# --- proceedings.py ------------------------------------------------------------


def test_update_proceeding_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    case = _make_case(db_session, "PR5-A", a.id)
    proceeding = Proceeding(
        case_id=case.id,
        court_name="AG Berlin",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
    )
    db_session.add(proceeding)
    db_session.commit()

    client = _login("b@example.com")
    resp = client.patch(
        f"/proceedings/{proceeding.id}", data={"court_name": "Hijacked"}
    )
    assert resp.status_code == 404


# --- costs.py ------------------------------------------------------------------


def test_costs_list_is_scoped_per_user(auth_enabled, db_session, two_users):
    a, b = two_users
    case_a = _make_case(db_session, "PR5-COST-A", a.id)
    case_b = _make_case(db_session, "PR5-COST-B", b.id)
    db_session.add_all(
        [
            LegalCost(
                case_id=case_a.id,
                category=CostCategory.GERICHTSKOSTEN,
                title="Cost owned by A",
                amount_net=100.0,
                amount_gross=100.0,
                status=CostStatus.OFFEN,
            ),
            LegalCost(
                case_id=case_b.id,
                category=CostCategory.GERICHTSKOSTEN,
                title="Cost owned by B",
                amount_net=200.0,
                amount_gross=200.0,
                status=CostStatus.OFFEN,
            ),
        ]
    )
    db_session.commit()

    client = _login("a@example.com")
    resp = client.get("/costs")
    assert resp.status_code == 200
    assert "Cost owned by A" in resp.text
    assert "Cost owned by B" not in resp.text


def test_create_cost_requires_edit_access_to_case(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-COST-A", a.id)

    client = _login("b@example.com")
    resp = client.post(
        "/costs",
        data={
            "case_id": "PR5-COST-A",
            "category": CostCategory.GERICHTSKOSTEN.value,
            "title": "Injected cost",
            "amount_net": "50",
        },
    )
    assert resp.status_code == 404


def test_mark_cost_paid_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    case_a = _make_case(db_session, "PR5-COST-A", a.id)
    cost = LegalCost(
        case_id=case_a.id,
        category=CostCategory.GERICHTSKOSTEN,
        title="A's cost",
        amount_net=100.0,
        amount_gross=100.0,
        status=CostStatus.OFFEN,
    )
    db_session.add(cost)
    db_session.commit()

    client = _login("b@example.com")
    resp = client.post(f"/costs/{cost.id}/pay")
    assert resp.status_code == 404


# --- chat.py ------------------------------------------------------------------


def test_conversation_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    doc = Document(title="A's doc", owner_id=a.id, case_id=None)
    db_session.add(doc)
    db_session.commit()

    client_a = _login("a@example.com")
    create_resp = client_a.post(
        "/api/chat/conversations",
        json={"scope_type": "document", "scope_id": str(doc.id)},
    )
    assert create_resp.status_code == 200
    conv_id = create_resp.json()["id"]

    client_b = _login("b@example.com")
    resp = client_b.get(f"/api/chat/conversations/{conv_id}")
    assert resp.status_code == 404


def test_chat_conversations_are_not_shared_between_users(
    auth_enabled, db_session, two_users
):
    """Two users chatting about the same case-visible document each get their
    own conversation — get_or_create must never hand user B user A's chat."""
    a, b = two_users
    case = _make_case(db_session, "PR5-CHAT", a.id)
    db_session.add(
        CaseShare(case_id=case.id, user_id=b.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.commit()

    client_a = _login("a@example.com")
    resp_a = client_a.post(
        "/api/chat/conversations",
        json={"scope_type": "case", "scope_id": case.id},
    )
    assert resp_a.status_code == 200
    conv_a_id = resp_a.json()["id"]

    client_b = _login("b@example.com")
    resp_b = client_b.post(
        "/api/chat/conversations",
        json={"scope_type": "case", "scope_id": case.id},
    )
    assert resp_b.status_code == 200
    conv_b_id = resp_b.json()["id"]

    assert conv_a_id != conv_b_id
    # B cannot read A's conversation directly either.
    assert client_b.get(f"/api/chat/conversations/{conv_a_id}").status_code == 404


# --- worker_queue.py -----------------------------------------------------------


def test_retry_failed_only_resets_callers_own_docs(auth_enabled, db_session, two_users):
    from unittest.mock import patch

    a, b = two_users
    doc_a = Document(
        title="A's failed doc",
        owner_id=a.id,
        case_id=None,
        pipeline_state=PipelineState.FAILED,
    )
    doc_b = Document(
        title="B's failed doc",
        owner_id=b.id,
        case_id=None,
        pipeline_state=PipelineState.FAILED,
    )
    db_session.add_all([doc_a, doc_b])
    db_session.commit()

    client = _login("a@example.com")
    with patch("app.tasks.dispatch.dispatch_task") as mock_dispatch:
        resp = client.post("/api/worker/queue/retry-failed")
    assert resp.status_code == 200

    dispatched_doc_ids = {call.args[1] for call in mock_dispatch.call_args_list}
    assert doc_a.id in dispatched_doc_ids
    assert doc_b.id not in dispatched_doc_ids


def test_worker_queue_badge_counts_exclude_other_users_failures(
    auth_enabled, db_session, two_users
):
    a, b = two_users
    db_session.add(
        Document(
            title="B's failed doc",
            owner_id=b.id,
            case_id=None,
            pipeline_state=PipelineState.FAILED,
        )
    )
    db_session.commit()

    client = _login("a@example.com")
    resp = client.get("/api/worker/queue/badge")
    assert resp.status_code == 200
    assert "1" not in resp.text or "n_failed" not in resp.text


# --- claims.py -----------------------------------------------------------------


def test_claim_precedent_toggle_404_for_non_owner(auth_enabled, db_session, two_users):
    from app.models.database import Claim, ClaimEvidence
    from app.models.enums import ClaimEvidenceRole, ClaimStatus

    a, b = two_users
    case_a = _make_case(db_session, "PR5-CLAIM-A", a.id)
    doc = Document(title="A's doc", owner_id=a.id, case_id=case_a.id)
    db_session.add(doc)
    db_session.flush()
    claim = Claim(claim_text="A fact", status=ClaimStatus.ASSERTED)
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=doc.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db_session.commit()

    client = _login("b@example.com")
    resp = client.post(f"/claims/{claim.id}/precedent/toggle")
    assert resp.status_code == 404


def test_claim_precedent_toggle_ok_for_owner(auth_enabled, db_session, two_users):
    from app.models.database import Claim, ClaimEvidence
    from app.models.enums import ClaimEvidenceRole, ClaimStatus

    a, b = two_users
    case_a = _make_case(db_session, "PR5-CLAIM-A", a.id)
    doc = Document(title="A's doc", owner_id=a.id, case_id=case_a.id)
    db_session.add(doc)
    db_session.flush()
    claim = Claim(claim_text="A fact", status=ClaimStatus.ASSERTED)
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=doc.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db_session.commit()

    client = _login("a@example.com")
    resp = client.post(f"/claims/{claim.id}/precedent/toggle")
    assert resp.status_code == 200
