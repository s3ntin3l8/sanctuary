import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, CaseStatus

client = TestClient(app)

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture
def test_case(db_session):
    """Create a test case directly in the DB."""
    case = Case(
        id="TEST-2024-001", title="Test Integration Case", status=CaseStatus.INTAKE
    )
    db_session.add(case)
    db_session.commit()
    return case


@pytest.mark.integration
def test_case_directory(test_case):
    """Verify the case appears in the case directory."""
    response = client.get("/cases")
    assert response.status_code == 200
    assert "TEST-2024-001" in response.text
    assert "Test Integration Case" in response.text


@pytest.mark.integration
def test_case_detail(test_case):
    """Verify the case detail page loads."""
    response = client.get(f"/cases/{test_case.id}")
    assert response.status_code == 200
    assert "Test Integration Case" in response.text


@pytest.mark.integration
def test_case_sidebar_counts(db_session):
    """Verify case appears in sidebar (landing page)."""
    # Create a document for the case so it shows up in activity/sidebar if needed
    case = Case(id="SIDEBAR-001", title="Sidebar Case", status=CaseStatus.ACTIVE)
    db_session.add(case)
    db_session.commit()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "SIDEBAR-001" in resp.text
