"""Integration tests for Phase 5d/5e/5f: case dashboard, brief panel, brief refresh."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case
from app.models.enums import CaseStatus, Jurisdiction

client = TestClient(app)


@pytest.mark.integration
def test_case_dashboard_renders_basic_structure(db_session):
    """GET /cases/{case_id} returns 200 with key UI strings."""
    case = Case(
        id="DASH-001",
        title="Dashboard Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    response = client.get("/cases/DASH-001")
    assert response.status_code == 200
    assert "Dashboard Test Case" in response.text
    assert "case-brief-panel" in response.text


@pytest.mark.integration
def test_case_dashboard_404_on_unknown_case():
    """GET /cases/NONEXISTENT returns 404."""
    response = client.get("/cases/NONEXISTENT-999-Z")
    assert response.status_code == 404


@pytest.mark.integration
def test_case_brief_partial_returns_panel(db_session):
    """GET /cases/{case_id}/brief returns the brief panel partial."""
    case = Case(
        id="BRIEF-001",
        title="Brief Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    response = client.get("/cases/BRIEF-001/brief")
    assert response.status_code == 200
    assert "case-brief-panel" in response.text


@pytest.mark.integration
def test_case_brief_refresh_sets_processing(db_session):
    """POST /cases/{case_id}/brief/refresh returns spinner state."""
    case = Case(
        id="REFRESH-001",
        title="Refresh Test Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()

    response = client.post("/cases/REFRESH-001/brief/refresh")
    assert response.status_code == 200
    assert "Generating brief" in response.text


@pytest.mark.integration
def test_case_dashboard_no_docs_no_brief_no_proceedings(db_session):
    """Dashboard must render without 500 for an empty case."""
    case = Case(
        id="EMPTY-001",
        title="Empty Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        ai_brief=None,
        total_cost_exposure=0,
    )
    db_session.add(case)
    db_session.commit()

    response = client.get("/cases/EMPTY-001")
    assert response.status_code == 200
    assert "Empty Case" in response.text


@pytest.mark.integration
def test_case_brief_partial_404_on_unknown_case():
    """GET /cases/NONE/brief returns 404."""
    response = client.get("/cases/NONE-999-X/brief")
    assert response.status_code == 404


@pytest.mark.integration
def test_case_brief_refresh_404_on_unknown_case():
    """POST /cases/NONE/brief/refresh returns 404."""
    response = client.post("/cases/NONE-999-X/brief/refresh")
    assert response.status_code == 404


@pytest.mark.integration
def test_case_dashboard_with_processing_brief(db_session):
    """Brief panel shows spinner when brief status is processing."""
    case = Case(
        id="PROC-001",
        title="Processing Brief Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        ai_brief={"status": "processing"},
    )
    db_session.add(case)
    db_session.commit()

    response = client.get("/cases/PROC-001")
    assert response.status_code == 200
    assert "Generating brief" in response.text
