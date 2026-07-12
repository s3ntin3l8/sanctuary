"""Integration tests for Phase 6 Truth Map routes."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, Claim, ClaimEvidence, Document
from app.models.enums import (
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    Jurisdiction,
    OriginatorType,
)

client = TestClient(app)


@pytest.fixture
def tm_case(db_session):
    case = Case(
        id="TM-ROUTE-001",
        title="Truth Map Route Test",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.flush()
    return case


@pytest.fixture
def tm_doc(db_session, tm_case):
    doc = Document(
        title="Test Document",
        content="content",
        case_id=tm_case.id,
        originator_type=OriginatorType.OPPOSING,
        sender="opposing@example.com",
        received_date=datetime(2025, 3, 1),
    )
    db_session.add(doc)
    db_session.flush()
    db_session.refresh(doc)
    return doc


def _make_claim_with_asserts(
    db_session,
    asserting_doc,
    text: str,
    claim_type: ClaimType = ClaimType.FACTUAL,
    status: ClaimStatus = ClaimStatus.ASSERTED,
):
    """Wave 2A helper: claim row + canonical ASSERTS evidence anchoring it
    to the document (and thus to the case via the document's case_id)."""
    claim = Claim(
        claim_text=text,
        claim_type=claim_type,
        status=status,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id,
            document_id=asserting_doc.id,
            role=ClaimEvidenceRole.ASSERTS,
        )
    )
    db_session.flush()
    db_session.refresh(claim)
    return claim


@pytest.fixture
def tm_claim_asserted(db_session, tm_case, tm_doc):
    return _make_claim_with_asserts(
        db_session, tm_doc, "Defendant was present at location"
    )


@pytest.fixture
def tm_claim_contested(db_session, tm_case, tm_doc):
    return _make_claim_with_asserts(
        db_session,
        tm_doc,
        "Contract was validly executed",
        claim_type=ClaimType.LEGAL,
        status=ClaimStatus.CONTESTED,
    )


# ---------------------------------------------------------------------------
# GET /cases/{case_id}/truthmap
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_truthmap_get_open_returns_200(db_session, tm_case, tm_claim_asserted):
    db_session.commit()
    response = client.get(f"/cases/{tm_case.id}/truthmap?filter=open")
    assert response.status_code == 200


@pytest.mark.integration
def test_truthmap_get_renders_claim_text(db_session, tm_case, tm_claim_asserted):
    db_session.commit()
    response = client.get(f"/cases/{tm_case.id}/truthmap?filter=open")
    assert "Defendant was present at location" in response.text


@pytest.mark.integration
def test_truthmap_get_shows_contested_claims(db_session, tm_case, tm_claim_contested):
    db_session.commit()
    response = client.get(f"/cases/{tm_case.id}/truthmap?filter=open")
    assert "Contract was validly executed" in response.text


@pytest.mark.integration
def test_truthmap_get_filter_established_returns_200(db_session, tm_case):
    db_session.commit()
    response = client.get(f"/cases/{tm_case.id}/truthmap?filter=established")
    assert response.status_code == 200


@pytest.mark.integration
def test_truthmap_get_unknown_case_returns_404():
    response = client.get("/cases/NONEXISTENT-TM-999/truthmap?filter=open")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /cases/{case_id}/claims/{claim_id}/status — user lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_status_mark_established(db_session, tm_case, tm_claim_asserted):
    db_session.commit()

    response = client.post(
        f"/cases/{tm_case.id}/claims/{tm_claim_asserted.id}/status",
        data={"status": "established"},
    )
    assert response.status_code == 200
    assert "established" in response.text.lower()
    assert 'id="truthmap-badge"' in response.text


@pytest.mark.integration
def test_post_status_reopen_from_established(db_session, tm_case, tm_doc):
    established_claim = _make_claim_with_asserts(
        db_session,
        tm_doc,
        "Established claim to reopen",
        status=ClaimStatus.ESTABLISHED,
    )
    db_session.commit()

    response = client.post(
        f"/cases/{tm_case.id}/claims/{established_claim.id}/status",
        data={"status": "asserted"},
    )
    assert response.status_code == 200
    assert "asserted" in response.text.lower()


@pytest.mark.integration
def test_post_status_contested_now_user_allowed(db_session, tm_case, tm_claim_asserted):
    # 'contested' is now user-settable (Fix #9); asserted → contested is valid.
    db_session.commit()

    response = client.post(
        f"/cases/{tm_case.id}/claims/{tm_claim_asserted.id}/status",
        data={"status": "contested"},
    )
    assert response.status_code == 200


@pytest.mark.integration
def test_post_status_refuted_returns_422(db_session, tm_case, tm_claim_asserted):
    db_session.commit()

    response = client.post(
        f"/cases/{tm_case.id}/claims/{tm_claim_asserted.id}/status",
        data={"status": "refuted"},
    )
    assert response.status_code == 422


@pytest.mark.integration
def test_post_status_claim_not_in_case_returns_404(db_session, tm_case, tm_doc):
    other_case = Case(
        id="TM-OTHER-001",
        title="Other Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    other_doc = Document(
        title="Other Doc",
        content="content",
        case_id="TM-OTHER-001",
        originator_type=OriginatorType.UNKNOWN,
        sender="x@x.com",
    )
    db_session.add(other_case)
    db_session.flush()
    db_session.add(other_doc)
    db_session.flush()
    other_claim = _make_claim_with_asserts(
        db_session, other_doc, "Belongs to another case"
    )
    db_session.commit()

    # Try to update other_claim via tm_case's URL — should 404
    response = client.post(
        f"/cases/{tm_case.id}/claims/{other_claim.id}/status",
        data={"status": "established"},
    )
    assert response.status_code == 404


@pytest.mark.integration
def test_post_status_invalid_value_returns_422(db_session, tm_case, tm_claim_asserted):
    """A status string that isn't a ClaimStatus member (not just a
    disallowed transition) hits the enum-parse branch, not transition_status."""
    db_session.commit()

    response = client.post(
        f"/cases/{tm_case.id}/claims/{tm_claim_asserted.id}/status",
        data={"status": "not-a-real-status"},
    )
    assert response.status_code == 422
    assert response.text == "Unknown status"


@pytest.mark.integration
def test_merge_batch_invalid_action_returns_422(db_session, tm_case):
    db_session.commit()

    response = client.post(
        f"/cases/{tm_case.id}/claims/proposals/merge/batch",
        data={"action": "not-a-real-action"},
    )
    assert response.status_code == 422
    assert response.text == "Unknown action"


# ---------------------------------------------------------------------------
# Case dashboard — Truth Map tab rendered on page load
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_case_dashboard_includes_truthmap_tab(db_session, tm_case, tm_claim_asserted):
    db_session.commit()
    response = client.get(f"/cases/{tm_case.id}")
    assert response.status_code == 200
    assert "Truth Map" in response.text
