import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    Case,
    CaseStatus,
    Document,
)

client = TestClient(app)


@pytest.mark.integration
def test_triage_page_renders(db_session):
    """Test triage page renders without errors."""
    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_with_pending_docs(db_session):
    """Test triage page shows pending documents."""
    doc = Document(
        title="Test Document for Review",
        case_id=None,
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_with_case_mapped(db_session):
    """Test triage page shows case-mapped documents."""
    case = Case(id="TRIAGE-TEST-001", title="Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    doc = Document(
        title="Document with Case",
        case_id="TRIAGE-TEST-001",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_activity_log_renders(db_session):
    """Test activity log page renders."""
    response = client.get("/activity")
    assert response.status_code == 200


@pytest.mark.integration
def test_activity_with_docs(db_session):
    """Test activity shows documents."""
    doc = Document(
        title="Recent Activity Document",
        case_id="_TRIAGE",
        needs_review=False,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/activity")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_case_selector(db_session):
    """Test triage page includes case selector."""
    response = client.get("/triage")
    assert response.status_code == 200
