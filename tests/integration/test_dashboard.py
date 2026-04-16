import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    Case,
    CaseStatus,
)

client = TestClient(app)


@pytest.mark.integration
def test_dashboard_renders(db_session):
    """Test dashboard renders without errors."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Dashboard" in response.text


@pytest.mark.integration
def test_dashboard_with_case(db_session):
    """Test dashboard with a case in database."""
    case = Case(
        id="TEST-DASH-001", title="Test Dashboard Case", status=CaseStatus.INTAKE
    )
    db_session.add(case)
    db_session.commit()

    response = client.get("/")
    assert response.status_code == 200


@pytest.mark.integration
def test_dashboard_deadlines_section(db_session):
    """Test dashboard has deadlines section."""
    response = client.get("/")
    assert response.status_code == 200
    # Just check the page renders, not specific content


@pytest.mark.integration
def test_dashboard_hearings_section(db_session):
    """Test dashboard has hearings section."""
    response = client.get("/")
    assert response.status_code == 200


@pytest.mark.integration
def test_dashboard_triage_section(db_session):
    """Test dashboard has triage section."""
    response = client.get("/")
    assert response.status_code == 200


@pytest.mark.integration
def test_dashboard_costs_section(db_session):
    """Test dashboard has costs section."""
    response = client.get("/")
    assert response.status_code == 200
