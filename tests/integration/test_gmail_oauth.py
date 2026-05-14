from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gmail_oauth_start_uses_session_and_redirects():
    from app.api.ingestion_settings import OAUTH_STATE_COOKIE, gmail_oauth_start

    flow = MagicMock()
    flow.authorization_url.return_value = ("https://accounts.google.test/auth", None)
    request = SimpleNamespace(session={})

    with patch("app.api.ingestion_settings.get_oauth_flow", return_value=flow):
        response = await gmail_oauth_start(request)

    assert response.status_code == 307
    assert response.headers["location"] == "https://accounts.google.test/auth"
    assert OAUTH_STATE_COOKIE in request.session
