import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.integration
def test_home_route():
    response = client.get("/")
    assert response.status_code == 200


@pytest.mark.integration
def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.integration
def test_cases_directory():
    response = client.get("/cases")
    assert response.status_code == 200


@pytest.mark.integration
def test_triage_page():
    response = client.get("/triage")
    assert response.status_code == 200


@pytest.mark.integration
def test_costs_page():
    response = client.get("/costs")
    assert response.status_code == 200


@pytest.mark.integration
def test_contacts_index_returns_404():
    """Index page deleted per vision §UI:382; only detail /contacts/{name} exists."""
    response = client.get("/contacts")
    assert response.status_code == 404


@pytest.mark.integration
def test_entities_returns_404():
    """Entities page deleted per vision §UI:383; ⌘K replaces it."""
    response = client.get("/entities")
    assert response.status_code == 404
