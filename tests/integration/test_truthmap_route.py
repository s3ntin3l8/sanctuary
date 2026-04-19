"""Integration tests for Phase 6 Truth Map routes."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, Claim, Document
from app.models.enums import (
    CaseStatus,
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


@pytest.fixture
def tm_claim_asserted(db_session, tm_case, tm_doc):
    claim = Claim(
        case_id=tm_case.id,
        source_document_id=tm_doc.id,
        claim_text="Defendant was present at location",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.flush()
    db_session.refresh(claim)
    return claim


@pytest.fixture
def tm_claim_contested(db_session, tm_case, tm_doc):
    claim = Claim(
        case_id=tm_case.id,
        source_document_id=tm_doc.id,
        claim_text="Contract was validly executed",
        claim_type=ClaimType.LEGAL,
        status=ClaimStatus.CONTESTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(claim)
    db_session.flush()
    db_session.refresh(claim)
    return claim


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
    established_claim = Claim(
        case_id=tm_case.id,
        source_document_id=tm_doc.id,
        claim_text="Established claim to reopen",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ESTABLISHED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(established_claim)
    db_session.commit()
    db_session.refresh(established_claim)

    response = client.post(
        f"/cases/{tm_case.id}/claims/{established_claim.id}/status",
        data={"status": "asserted"},
    )
    assert response.status_code == 200
    assert "asserted" in response.text.lower()


@pytest.mark.integration
def test_post_status_ai_owned_state_returns_422(db_session, tm_case, tm_claim_asserted):
    db_session.commit()

    response = client.post(
        f"/cases/{tm_case.id}/claims/{tm_claim_asserted.id}/status",
        data={"status": "contested"},
    )
    assert response.status_code == 422


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
    other_claim = Claim(
        case_id="TM-OTHER-001",
        source_document_id=other_doc.id,
        claim_text="Belongs to another case",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db_session.add(other_claim)
    db_session.commit()
    db_session.refresh(other_claim)

    # Try to update other_claim via tm_case's URL — should 404
    response = client.post(
        f"/cases/{tm_case.id}/claims/{other_claim.id}/status",
        data={"status": "established"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Case dashboard — Truth Map tab rendered on page load
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_case_dashboard_includes_truthmap_tab(db_session, tm_case, tm_claim_asserted):
    db_session.commit()
    response = client.get(f"/cases/{tm_case.id}")
    assert response.status_code == 200
    assert "Truth Map" in response.text
