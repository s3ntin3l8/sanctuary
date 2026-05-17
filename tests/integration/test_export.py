"""Integration tests for the GDPR export endpoint."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core.rate_limit import limiter
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset the in-memory rate limiter before each test."""
    limiter.reset()
    yield


@pytest.mark.integration
def test_export_returns_zip(db_session):
    response = client.get("/api/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "sanctuary_export_" in response.headers["content-disposition"]
    # Verify it's a valid zip
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    names = zf.namelist()
    assert "manifest.json" in names
    assert "README.md" in names
    # At least the user_settings table should be exported
    assert any(n.startswith("data/") for n in names)


@pytest.mark.integration
def test_export_manifest_has_table_counts(db_session):
    response = client.get("/api/export")
    assert response.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert "table_counts" in manifest
    assert "export_date" in manifest
    assert isinstance(manifest["table_counts"], dict)
