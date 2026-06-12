"""HTTP-boundary integration tests for settings routers.

Covers:
- settings_maintenance  (/api/settings/maintenance/*)
- settings_appearance   (/api/settings/theme, /timezone, /dashboard-cards)
- settings_parties      (/api/settings/parties)
- ingestion_settings    (/api/ingest/settings/update)
- settings_ai_config    (/api/settings/ai/*)
- settings_page         (GET /settings/*)
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import AppSettings, AuditLog, UserSettings
from app.models.enums import AuditEventType

client = TestClient(app)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_maintenance_reset_ai_enrichment(db_session):
    """POST /api/settings/maintenance/reset-enrichment returns 200 HTML and writes audit log."""
    response = client.post("/api/settings/maintenance/reset-enrichment")
    assert response.status_code == 200
    # Response is an HTML fragment containing a count
    assert "Reset" in response.text

    db_session.expire_all()
    log = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.MAINTENANCE_RESET_AI_ENRICHMENT)
        .first()
    )
    assert log is not None, (
        "Expected audit log entry for maintenance_reset_ai_enrichment"
    )


@pytest.mark.integration
def test_maintenance_clear_all_data(db_session, sample_case):
    """POST /api/settings/maintenance/clear-all-data returns 200 HTML and writes audit log."""
    response = client.post("/api/settings/maintenance/clear-all-data")
    assert response.status_code == 200
    assert "Cleared" in response.text

    db_session.expire_all()
    log = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.MAINTENANCE_CLEAR_ALL_DATA)
        .first()
    )
    assert log is not None, "Expected audit log entry for maintenance_clear_all_data"


# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_appearance_save_timezone_valid(db_session):
    """POST /api/settings/timezone with a valid tz returns 204 and persists."""
    response = client.post("/api/settings/timezone", data={"tz": "Europe/Berlin"})
    assert response.status_code == 204

    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    assert settings is not None
    assert settings.settings_json.get("timezone") == "Europe/Berlin"


@pytest.mark.integration
def test_appearance_save_timezone_invalid():
    """POST /api/settings/timezone with an invalid tz returns 422."""
    response = client.post("/api/settings/timezone", data={"tz": "Not/AReal/Timezone"})
    assert response.status_code == 422


@pytest.mark.integration
def test_appearance_save_theme(db_session):
    """POST /api/settings/theme persists the theme choice."""
    response = client.post("/api/settings/theme", data={"theme": "light"})
    assert response.status_code == 204

    db_session.expire_all()
    settings = db_session.query(UserSettings).first()
    assert settings is not None
    assert settings.settings_json.get("theme") == "light"


@pytest.mark.integration
def test_appearance_save_dashboard_cards(db_session):
    """POST /api/settings/dashboard-cards persists card visibility flags."""
    response = client.post(
        "/api/settings/dashboard-cards",
        data={"action_items": "on", "costs": "off", "documents": "on"},
    )
    assert response.status_code == 204

    db_session.expire_all()
    settings = db_session.query(UserSettings).first()
    assert settings is not None
    cards = settings.settings_json.get("dashboard_cards", {})
    assert cards.get("action_items") is True
    assert cards.get("costs") is False
    assert cards.get("documents") is True


# ---------------------------------------------------------------------------
# Parties / identity
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_parties_set_identity(db_session):
    """POST /api/settings/parties persists own_self and own_parties."""
    response = client.post(
        "/api/settings/parties",
        data={"own_self": "Alice Müller", "own_parties": "Alice Müller, Firma GmbH"},
    )
    assert response.status_code == 204

    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    assert settings is not None
    identity = settings.settings_json.get("party_identity", {})
    assert identity.get("own_self") == "Alice Müller"
    assert "Firma GmbH" in identity.get("own_parties", [])


@pytest.mark.integration
def test_parties_set_identity_empty(db_session):
    """POST /api/settings/parties with empty fields succeeds (no validation error)."""
    response = client.post(
        "/api/settings/parties", data={"own_self": "", "own_parties": ""}
    )
    assert response.status_code == 204

    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    assert settings is not None
    identity = settings.settings_json.get("party_identity", {})
    assert identity.get("own_self") == ""
    assert identity.get("own_parties") == []


# ---------------------------------------------------------------------------
# Ingestion settings
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ingestion_update_settings(db_session):
    """POST /api/ingest/settings/update persists allowlist and label_filter."""
    response = client.post(
        "/api/ingest/settings/update",
        data={
            "allowlist": "sender@example.com, other@example.com",
            "label_filter": "INBOX",
        },
        follow_redirects=False,
    )
    # Endpoint redirects to /settings/gmail on success
    assert response.status_code in (303, 302, 200)

    db_session.expire_all()
    # Gmail is per-user now: it lands in the (dev-mode) bootstrap admin's settings.
    from app.services import auth_service

    admin = auth_service.get_or_create_bootstrap_admin(db_session)
    s_json = auth_service.ensure_user_settings(db_session, admin).settings_json
    assert "sender@example.com" in s_json.get("gmail_allowlist", [])
    assert s_json.get("gmail_label_filter") == "INBOX"


@pytest.mark.integration
def test_ingestion_update_settings_empty_allowlist(db_session):
    """POST /api/ingest/settings/update with empty allowlist stores empty list."""
    response = client.post(
        "/api/ingest/settings/update",
        data={"allowlist": "", "label_filter": ""},
        follow_redirects=False,
    )
    assert response.status_code in (303, 302, 200)

    db_session.expire_all()
    from app.services import auth_service

    admin = auth_service.get_or_create_bootstrap_admin(db_session)
    s_json = auth_service.ensure_user_settings(db_session, admin).settings_json
    assert s_json.get("gmail_allowlist") == []


# ---------------------------------------------------------------------------
# AI config
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ai_config_save_user_context(db_session):
    """POST /api/settings/ai/user-context persists context and returns HTML toast."""
    response = client.post(
        "/api/settings/ai/user-context",
        data={"user_context": "Always respond in German."},
    )
    assert response.status_code == 200
    assert "saved" in response.text.lower() or "Context" in response.text

    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    assert settings is not None
    ai = settings.settings_json.get("ai", {})
    assert ai.get("user_context") == "Always respond in German."


@pytest.mark.integration
def test_ai_config_create_instance(db_session):
    """POST /api/settings/ai/instances creates an instance entry without probing embed dim."""
    # No embed_model → skips embed dim probe; health probe will fail gracefully.
    response = client.post(
        "/api/settings/ai/instances",
        data={
            "label": "Test Instance",
            "base_url": "http://127.0.0.1:11434",
            "api_key": "not-needed",
            "summary_model": "llama3",
            "embed_model": "",
        },
    )
    # Returns an HTML fragment (instance row) or an error toast; either way not a server crash
    assert response.status_code == 200

    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    assert settings is not None
    instances = settings.settings_json.get("ai", {}).get("instances", [])
    labels = [i.get("label") for i in instances]
    assert "Test Instance" in labels, f"Expected 'Test Instance' in {labels}"
    # Provider is no longer user-set — stored as auto for runtime detection.
    created = next(i for i in instances if i.get("label") == "Test Instance")
    assert created.get("provider") == "auto"


@pytest.mark.integration
def test_ai_config_delete_nonexistent_instance():
    """DELETE /api/settings/ai/instances/{id} for unknown id returns toast HTML."""
    response = client.delete("/api/settings/ai/instances/inst_nonexistent")
    # Service returns 200 with empty body (row swapped out) or 200 with error toast
    assert response.status_code in (200, 404)


@pytest.mark.integration
def test_ai_config_set_role_invalid_role():
    """POST /api/settings/ai/role/{role} with invalid role returns 400."""
    response = client.post(
        "/api/settings/ai/role/invalid",
        data={"instance_id": "inst_abc"},
    )
    assert response.status_code == 400


@pytest.mark.integration
def test_ai_config_set_role_missing_instance():
    """POST /api/settings/ai/role/{role} with unknown instance_id returns 404."""
    response = client.post(
        "/api/settings/ai/role/chat",
        data={"instance_id": "inst_doesnotexist"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Settings page renders
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_settings_page_redirects():
    """GET /settings redirects to the first settings tab (Account)."""
    response = client.get("/settings", follow_redirects=False)
    assert response.status_code in (301, 302, 303)
    assert "account" in response.headers.get("location", "")


@pytest.mark.integration
def test_settings_gmail_page_renders():
    """GET /settings/gmail returns 200 with HTML content."""
    response = client.get("/settings/gmail")
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text or "<html" in response.text.lower()


@pytest.mark.integration
def test_settings_ai_page_renders():
    """GET /settings/ai returns 200 with HTML content."""
    response = client.get("/settings/ai")
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text or "<html" in response.text.lower()


@pytest.mark.integration
def test_settings_appearance_page_renders():
    """GET /settings/appearance returns 200 with HTML content."""
    response = client.get("/settings/appearance")
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text or "<html" in response.text.lower()


@pytest.mark.integration
def test_settings_data_page_renders():
    """GET /settings/data returns 200 with HTML content."""
    response = client.get("/settings/data")
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text or "<html" in response.text.lower()


@pytest.mark.integration
def test_settings_parties_page_renders():
    """GET /settings/parties returns 200 with HTML content."""
    response = client.get("/settings/parties")
    assert response.status_code == 200
    assert "<!DOCTYPE html>" in response.text or "<html" in response.text.lower()
