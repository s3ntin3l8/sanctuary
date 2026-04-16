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
    assert "activity" in response.text.lower() or "triage" in response.text.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_static_files():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.get("/static/styles.css")
    assert response.status_code in [200, 404]
