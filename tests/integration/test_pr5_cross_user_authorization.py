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
    client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
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
    resp = client.delete(f"/api/v1/documents/{doc.id}")
    assert resp.status_code == 404


def test_delete_document_ok_for_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    doc = Document(title="A's doc", owner_id=a.id, case_id=None)
    db_session.add(doc)
    db_session.commit()

    client = _login("a@example.com")
    resp = client.delete(f"/api/v1/documents/{doc.id}")
    assert resp.status_code == 204


def test_document_original_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    doc = Document(title="A's doc", owner_id=a.id, case_id=None, file_path=None)
    db_session.add(doc)
    db_session.commit()

    client = _login("b@example.com")
    resp = client.get(f"/api/v1/documents/{doc.id}/original")
    assert resp.status_code == 404


def test_upload_filename_case_id_sniffing_cannot_plant_into_another_users_case(
    auth_enabled, db_session, two_users, mock_dispatch_task
):
    """Naming a file after a real case id must not silently file it there
    unless the uploader can edit that case — the case_id form field is
    checked by the route, but ingest_file() also sniffs a case id straight
    out of the filename when no case_id is submitted."""
    a, b = two_users
    _make_case(db_session, "ADV-999-Z", b.id)

    client = _login("a@example.com")
    file_content = b"forged content"
    resp = client.post(
        "/api/v1/upload",
        files=[("files", ("ADV-999-Z-forged-letter.txt", file_content, "text/plain"))],
    )
    assert resp.status_code in (200, 302, 303)

    db_session.expire_all()
    planted = db_session.query(Document).filter(Document.case_id == "ADV-999-Z").first()
    assert planted is None


# --- cases.py ----------------------------------------------------------------


def test_update_case_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)

    client = _login("b@example.com")
    resp = client.patch("/api/v1/cases/PR5-A", json={"title": "Hijacked"})
    assert resp.status_code == 404


def test_update_case_ok_for_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)

    client = _login("a@example.com")
    resp = client.patch("/api/v1/cases/PR5-A", json={"title": "Renamed"})
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
    assert client.get("/api/v1/cases/PR5-A").status_code == 200
    resp = client.patch("/api/v1/cases/PR5-A", json={"title": "Hijacked"})
    assert resp.status_code == 404


def test_update_case_ok_for_editor_share(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)
    db_session.add(
        CaseShare(case_id="PR5-A", user_id=b.id, permission=CaseAccessLevel.EDITOR)
    )
    db_session.commit()

    client = _login("b@example.com")
    resp = client.patch("/api/v1/cases/PR5-A", json={"title": "Edited by editor"})
    assert resp.status_code == 200


def test_purge_case_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-A", a.id)

    client = _login("b@example.com")
    resp = client.post("/api/v1/cases/PR5-A/purge", json={"confirm": "purge PR5-A"})
    assert resp.status_code == 404


def test_create_case_owner_can_immediately_edit_it(auth_enabled, db_session, two_users):
    """Regression guard: create_case must stamp owner_id, or the creator would
    be locked out of their own case the moment PR5's edit guard is wired up."""
    a, _b = two_users
    client = _login("a@example.com")
    resp = client.post(
        "/api/v1/cases",
        json={"case_id": "PR5-NEW", "title": "New Case", "court_name": "AG Berlin"},
    )
    assert resp.status_code == 201
    db_session.expire_all()
    case = db_session.get(Case, "PR5-NEW")
    assert case is not None
    assert case.owner_id == a.id

    edit_resp = client.patch(
        "/api/v1/cases/PR5-NEW", json={"title": "Renamed by owner"}
    )
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
        f"/api/v1/proceedings/{proceeding.id}", json={"court_name": "Hijacked"}
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
    resp = client.get("/api/v1/costs")
    assert resp.status_code == 200
    titles = {c["title"] for g in resp.json()["cases"] for c in g["costs"]}
    assert "Cost owned by A" in titles
    assert "Cost owned by B" not in titles


def test_create_cost_requires_edit_access_to_case(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "PR5-COST-A", a.id)

    client = _login("b@example.com")
    resp = client.post(
        "/api/v1/cases/PR5-COST-A/costs",
        json={
            "category": CostCategory.GERICHTSKOSTEN.value,
            "title": "Injected cost",
            "amount_net": 50,
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
    resp = client.post(f"/api/v1/costs/{cost.id}/pay")
    assert resp.status_code == 404


# --- chat.py ------------------------------------------------------------------


def test_conversation_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    doc = Document(title="A's doc", owner_id=a.id, case_id=None)
    db_session.add(doc)
    db_session.commit()

    client_a = _login("a@example.com")
    create_resp = client_a.post(
        "/api/v1/chat/conversations",
        json={"scope_type": "document", "scope_id": str(doc.id)},
    )
    assert create_resp.status_code == 200
    conv_id = create_resp.json()["id"]

    client_b = _login("b@example.com")
    resp = client_b.get(f"/api/v1/chat/conversations/{conv_id}")
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
        "/api/v1/chat/conversations",
        json={"scope_type": "case", "scope_id": case.id},
    )
    assert resp_a.status_code == 200
    conv_a_id = resp_a.json()["id"]

    client_b = _login("b@example.com")
    resp_b = client_b.post(
        "/api/v1/chat/conversations",
        json={"scope_type": "case", "scope_id": case.id},
    )
    assert resp_b.status_code == 200
    conv_b_id = resp_b.json()["id"]

    assert conv_a_id != conv_b_id
    # B cannot read A's conversation directly either.
    assert client_b.get(f"/api/v1/chat/conversations/{conv_a_id}").status_code == 404


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
    # The failed-count pill (bg-error) only renders when n_failed > 0 —
    # its absence is the real signal that B's failure wasn't counted for A.
    assert "bg-error" not in resp.text


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
    resp = client.post(f"/api/v1/claims/{claim.id}/precedent")
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
    resp = client.post(f"/api/v1/claims/{claim.id}/precedent")
    assert resp.status_code == 200


def test_update_claim_status_cannot_be_used_via_own_case_to_mutate_other_case_claim(
    auth_enabled, db_session, two_users
):
    """A claim can be evidence-linked from documents in two different cases
    (claims are global). Edit access to one linked case must not be enough
    to mutate the claim's globally-shared status."""
    from app.models.database import Claim, ClaimEvidence
    from app.models.enums import ClaimEvidenceRole, ClaimStatus

    a, b = two_users
    case_a = _make_case(db_session, "PR5-CLAIM-A", a.id)
    case_b = _make_case(db_session, "PR5-CLAIM-B", b.id)
    doc_a = Document(title="A's doc", owner_id=a.id, case_id=case_a.id)
    doc_b = Document(title="B's doc", owner_id=b.id, case_id=case_b.id)
    db_session.add_all([doc_a, doc_b])
    db_session.flush()
    claim = Claim(claim_text="Shared fact", status=ClaimStatus.ASSERTED)
    db_session.add(claim)
    db_session.flush()
    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=claim.id, document_id=doc_a.id, role=ClaimEvidenceRole.ASSERTS
            ),
            ClaimEvidence(
                claim_id=claim.id,
                document_id=doc_b.id,
                role=ClaimEvidenceRole.SUPPORTS,
            ),
        ]
    )
    db_session.commit()

    client = _login("a@example.com")
    resp = client.put(
        f"/api/v1/claims/{claim.id}/status",
        json={"status": ClaimStatus.ESTABLISHED.value},
    )
    assert resp.status_code == 404

    db_session.expire_all()
    assert db_session.get(Claim, claim.id).status == ClaimStatus.ASSERTED


def test_batch_merge_proposal_cannot_confirm_via_unrelated_case_edit_access(
    auth_enabled, db_session, two_users
):
    """Bulk-confirming merge proposals for a case the caller can edit must not
    silently confirm a merge whose *other* claim only has evidence in a case
    the caller can't touch."""
    from app.models.database import Claim, ClaimEvidence, ClaimMergeProposal
    from app.models.enums import (
        ClaimEvidenceRole,
        ClaimStatus,
        ProposalConfidence,
        ProposalStatus,
    )

    a, b = two_users
    case_a = _make_case(db_session, "PR5-MERGE-A", a.id)
    case_b = _make_case(db_session, "PR5-MERGE-B", b.id)
    doc_a = Document(title="A's doc", owner_id=a.id, case_id=case_a.id)
    doc_b = Document(title="B's doc", owner_id=b.id, case_id=case_b.id)
    db_session.add_all([doc_a, doc_b])
    db_session.flush()

    new_claim = Claim(claim_text="New claim", status=ClaimStatus.ASSERTED)
    existing_claim = Claim(claim_text="Existing claim", status=ClaimStatus.ASSERTED)
    db_session.add_all([new_claim, existing_claim])
    db_session.flush()
    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=new_claim.id,
                document_id=doc_a.id,
                role=ClaimEvidenceRole.ASSERTS,
            ),
            ClaimEvidence(
                claim_id=existing_claim.id,
                document_id=doc_b.id,
                role=ClaimEvidenceRole.ASSERTS,
            ),
        ]
    )
    proposal = ClaimMergeProposal(
        new_claim_id=new_claim.id,
        existing_claim_id=existing_claim.id,
        confidence=ProposalConfidence.HIGH,
        status=ProposalStatus.PENDING,
    )
    db_session.add(proposal)
    db_session.commit()

    client = _login("a@example.com")
    resp = client.post(
        f"/api/v1/cases/{case_a.id}/claims/proposals/merge", json={"action": "confirm"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"confirmed": 0, "dismissed": 0}

    db_session.expire_all()
    assert db_session.get(ClaimMergeProposal, proposal.id).status == (
        ProposalStatus.PENDING
    )
    assert db_session.get(Claim, new_claim.id) is not None


# --- chat.py conversation scope revocation --------------------------------------


def test_conversation_inaccessible_after_case_share_revoked(
    auth_enabled, db_session, two_users
):
    """A conversation created while B had a VIEWER share on the case must stop
    working once that share is revoked — ownership of the Conversation row
    alone shouldn't keep retrieving from a case B can no longer see."""
    a, b = two_users
    case = _make_case(db_session, "PR5-CHAT-REVOKE", a.id)
    share = CaseShare(case_id=case.id, user_id=b.id, permission=CaseAccessLevel.VIEWER)
    db_session.add(share)
    db_session.commit()

    client_b = _login("b@example.com")
    create_resp = client_b.post(
        "/api/v1/chat/conversations",
        json={"scope_type": "case", "scope_id": case.id},
    )
    assert create_resp.status_code == 200
    conv_id = create_resp.json()["id"]
    assert client_b.get(f"/api/v1/chat/conversations/{conv_id}").status_code == 200

    db_session.delete(db_session.get(CaseShare, share.id))
    db_session.commit()

    resp = client_b.get(f"/api/v1/chat/conversations/{conv_id}")
    assert resp.status_code == 404


def test_relationship_decision_requires_edit_access_to_both_documents(
    auth_enabled, db_session, two_users
):
    """B edits case A1 (shared) but not A2: a relationship spanning both
    must be 404 for B, since confirming/rejecting mutates the far side too."""
    from app.models.database import DocumentRelationship
    from app.models.enums import RelationshipConfidence, RelationshipType

    a, b = two_users
    shared = _make_case(db_session, "PR5-REL-SHARED", a.id)
    private = _make_case(db_session, "PR5-REL-PRIVATE", a.id)
    db_session.add(
        CaseShare(case_id=shared.id, user_id=b.id, permission=CaseAccessLevel.EDITOR)
    )
    near = Document(title="shared doc", owner_id=a.id, case_id=shared.id)
    far = Document(title="private doc", owner_id=a.id, case_id=private.id)
    db_session.add_all([near, far])
    db_session.flush()
    spanning = DocumentRelationship(
        from_document_id=near.id,
        to_document_id=far.id,
        relationship_type=RelationshipType.REFERENCES,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(spanning)
    db_session.commit()

    client = _login("b@example.com")
    assert (
        client.post(
            f"/api/v1/documents/relationships/{spanning.id}/confirm"
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/documents/relationships/{spanning.id}").status_code
        == 404
    )
    db_session.expire_all()
    assert (
        db_session.get(DocumentRelationship, spanning.id).confidence
        == RelationshipConfidence.AI_DETECTED
    )


def test_viewer_share_can_read_but_not_pin(auth_enabled, db_session, two_users):
    """A VIEWER share reads the reader but gets 404 on pin create/update/delete."""
    from app.models.database import DocumentPin

    a, b = two_users
    case = _make_case(db_session, "PR5-PIN-VIEW", a.id)
    db_session.add(
        CaseShare(case_id=case.id, user_id=b.id, permission=CaseAccessLevel.VIEWER)
    )
    doc = Document(
        title="pinnable",
        owner_id=a.id,
        case_id=case.id,
        content="The order is final.",
        key_passages=[
            {"id": "passage-one1", "text": "The order is final.", "kind": "ruling"}
        ],
    )
    db_session.add(doc)
    db_session.flush()
    pin = DocumentPin(
        document_id=doc.id, passage_id="passage-one1", note="a's", user_id=a.id
    )
    db_session.add(pin)
    db_session.commit()

    client = _login("b@example.com")
    reader = client.get(f"/api/v1/documents/{doc.id}/reader")
    assert reader.status_code == 200
    assert [p["id"] for p in reader.json()["pins"]] == [pin.id]
    assert (
        client.post(
            f"/api/v1/documents/{doc.id}/pins",
            json={"passage_id": "passage-one1", "note": "b's"},
        ).status_code
        == 404
    )
    assert (
        client.patch(f"/api/v1/pins/{pin.id}", json={"note": "edited"}).status_code
        == 404
    )
    assert client.delete(f"/api/v1/pins/{pin.id}").status_code == 404
    db_session.expire_all()
    assert db_session.get(DocumentPin, pin.id).note == "a's"
