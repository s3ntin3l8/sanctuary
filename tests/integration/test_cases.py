import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.integration
def test_case_directory_empty():
    """Verify the case directory works when empty."""
    response = client.get("/cases")
    assert response.status_code == 200


@pytest.mark.integration
def test_dashboard_loads():
    """Verify dashboard loads."""
    response = client.get("/")
    assert response.status_code == 200
