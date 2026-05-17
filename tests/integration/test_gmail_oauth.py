from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.integration
def test_gmail_oauth_start_uses_session_and_redirects():
    """GET /api/ingest/gmail/oauth/start redirects to the Google OAuth authorization URL."""
    flow = MagicMock()
    flow.authorization_url.return_value = ("https://accounts.google.test/auth", None)

    with patch("app.api.ingestion_settings.get_oauth_flow", return_value=flow):
        response = client.get("/api/ingest/gmail/oauth/start", follow_redirects=False)

    assert response.status_code in (302, 303, 307, 308)
    assert "accounts.google.test" in response.headers["location"]
