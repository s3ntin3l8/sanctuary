import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_favicon_is_served():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
