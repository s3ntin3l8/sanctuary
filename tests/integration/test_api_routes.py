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
def test_activity_page():
    response = client.get("/activity")
    assert response.status_code == 200


@pytest.mark.integration
def test_costs_page():
    response = client.get("/costs")
    assert response.status_code == 200


@pytest.mark.integration
def test_contacts_page():
    response = client.get("/contacts")
    assert response.status_code == 200
