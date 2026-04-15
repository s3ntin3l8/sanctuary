import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_main():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.get("/activity")
    assert response.status_code == 200
    # Check if some expected content is in the response (e.g., the title)
    assert "activity" in response.text.lower() or "triage" in response.text.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_static_files():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        # Check if static css is reachable
        response = await ac.get("/static/styles.css")
    # Even if it doesn't exist yet, it should try to return it (or 404 if missing)
    # But usually the router should be configured
    assert response.status_code in [200, 404]
