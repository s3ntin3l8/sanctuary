"""HTTP-boundary integration tests for settings routers.

Covers:
- settings_maintenance  (/api/settings/maintenance/*)
- settings_appearance   (/api/settings/theme, /timezone, /dashboard-cards)
- settings_parties      (/api/settings/parties)
- ingestion_settings    (/api/ingest/settings/update)
- settings_ai_config    (/api/settings/ai/*)
- settings_page         (GET /settings/*)
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import AppSettings, AuditLog, Case, Entity, User, UserSettings
from app.models.enums import AuditEventType, EntityType

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
    """POST /api/settings/maintenance/clear-all-data returns 200 HTML, writes an
    audit log, wipes workspace data — and preserves the account, its settings,
    and global app/AI config (regression test: the endpoint used to delete
    `users`, which cascade-deleted `user_settings`, and deleted `app_settings`
    outright, contradicting the UI's "preserves user settings, including
    connected accounts and API keys" promise)."""
    admin_id = db_session.query(User).filter_by(email="admin@localhost").one().id
    app_settings_id = db_session.query(AppSettings).first().id
    case_id = sample_case.id  # capture before the clear expires/deletes the row

    # Bulk-insert enough workspace data that VACUUM has real, measurable free
    # space to reclaim — a handful of rows leaves no freelist pages to shrink,
    # which would let a broken/no-op VACUUM pass silently.
    db_session.bulk_save_objects(
        [
            Entity(
                case_id=case_id, type=EntityType.PERSON, name=f"Test Entity {i}" * 20
            )
            for i in range(3000)
        ]
    )
    db_session.commit()

    db_path = db_session.get_bind().url.database
    size_before = os.path.getsize(db_path)

    response = client.post("/api/settings/maintenance/clear-all-data")
    assert response.status_code == 200
    assert "Cleared" in response.text

    size_after = os.path.getsize(db_path)
    assert size_after < size_before, (
        f"clear-all-data must VACUUM and shrink the DB file: "
        f"before={size_before} after={size_after}"
    )

    db_session.expire_all()

    log = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.MAINTENANCE_CLEAR_ALL_DATA)
        .first()
    )
    assert log is not None, "Expected audit log entry for maintenance_clear_all_data"

    # Workspace/domain data is gone.
    assert db_session.query(Case).filter_by(id=case_id).first() is None

    # Account, per-user settings, and global app/AI config survive.
    admin = db_session.query(User).filter_by(id=admin_id).first()
    assert admin is not None, "clear-all-data must not delete the users table"
    assert (
        db_session.query(UserSettings).filter_by(user_id=admin_id).first() is not None
    ), "user_settings must survive (was cascade-deleted via users FK)"
    assert (
        db_session.query(AppSettings).filter_by(id=app_settings_id).first() is not None
    ), "app_settings (AI config / API keys / connected accounts) must survive"


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
def test_ai_config_create_instance_refreshes_role_cards(db_session):
    """POST /api/settings/ai/instances OOB-refreshes the empty-state
    placeholder and all three role cards in the same response, so the new
    endpoint is selectable in Chat/Embeddings/OCR without a page refresh.

    Regression guard — the response used to be a single appended row with no
    OOB fragments, leaving "No endpoints configured" and the stale role
    selectors in place until a manual reload.
    """
    response = client.post(
        "/api/settings/ai/instances",
        data={
            "label": "OOB Test Instance",
            "base_url": "http://127.0.0.1:11434",
            "api_key": "not-needed",
            "summary_model": "llama3",
            "embed_model": "",
        },
    )
    assert response.status_code == 200

    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    instances = settings.settings_json.get("ai", {}).get("instances", [])
    created = next(i for i in instances if i.get("label") == "OOB Test Instance")

    # Empty-state placeholder is OOB-deleted (idempotent when already absent).
    assert 'id="ai-empty" hx-swap-oob="delete"' in response.text
    # All three role cards are OOB-replaced by id.
    for role in ("chat", "embed", "ocr"):
        assert f'id="role-card-{role}"' in response.text
    assert response.text.count('hx-swap-oob="true"') >= 3
    # The new instance is an option in every role's endpoint selector.
    assert response.text.count(f'value="{created["id"]}"') >= 3


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


@pytest.mark.integration
def test_ai_config_set_role_embed_persists_model_on_probe_failure(db_session):
    """2B: a chosen embed model is saved even when the dim probe fails.

    Regression guard — the route used to early-return and discard the user's
    pick if _probe_embed_dim failed and no dim was known.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    client.post(
        "/api/settings/ai/instances",
        data={
            "label": "EmbedBox",
            "base_url": "http://127.0.0.1:11434",
            "api_key": "not-needed",
            "summary_model": "",
            "embed_model": "",
        },
    )
    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    inst = next(
        i
        for i in settings.settings_json["ai"]["instances"]
        if i.get("label") == "EmbedBox"
    )
    inst_id = inst["id"]

    fake_provider = MagicMock()
    fake_provider.probe_health = AsyncMock(return_value={"ok": False, "detail": "down"})
    fake_provider.get_type = AsyncMock(return_value="ollama")

    with (
        patch(
            "app.api.settings_ai_config._probe_embed_dim",
            AsyncMock(return_value=(None, "unreachable")),
        ),
        patch("app.api.settings_ai_config._provider_for", return_value=fake_provider),
    ):
        resp = client.post(
            "/api/settings/ai/role/embed",
            data={"instance_id": inst_id, "model": "nomic-embed"},
        )
    assert resp.status_code == 200
    # OOB header refresh (§3): the saved model is swapped into the resting label.
    assert "role-model-label-embed" in resp.text
    assert "nomic-embed" in resp.text
    # Regression guard — an embed model change used to leave the Embedding
    # Index section's Dim/Model stale until a manual page refresh.
    assert 'id="embed-index-dim" hx-swap-oob="true"' in resp.text
    assert 'id="embed-index-model" hx-swap-oob="true"' in resp.text

    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    saved = next(
        i for i in settings.settings_json["ai"]["instances"] if i["id"] == inst_id
    )
    assert saved["embed_model"] == "nomic-embed"


# ---------------------------------------------------------------------------
# AI worker concurrency
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_set_worker_concurrency_valid(db_session):
    """POST /api/settings/ai/worker-concurrency persists + emits an audit event."""
    from unittest.mock import patch

    with patch(
        "app.services.worker_control.apply_ai_concurrency",
        return_value={"live": False, "nodes": []},
    ):
        response = client.post(
            "/api/settings/ai/worker-concurrency", data={"concurrency": "6"}
        )
    assert response.status_code == 200

    db_session.expire_all()
    settings = db_session.query(AppSettings).first()
    assert settings.settings_json.get("workers", {}).get("ai_concurrency") == 6
    log = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.SETTINGS_WORKERS_CHANGED)
        .first()
    )
    assert log is not None


@pytest.mark.integration
def test_set_worker_concurrency_out_of_bounds():
    """Out-of-range value is rejected with 400 (no worker call needed)."""
    response = client.post(
        "/api/settings/ai/worker-concurrency", data={"concurrency": "99"}
    )
    assert response.status_code == 400


@pytest.mark.integration
def test_set_worker_concurrency_non_integer():
    """Non-integer form value is rejected by FastAPI coercion (422)."""
    response = client.post(
        "/api/settings/ai/worker-concurrency", data={"concurrency": "abc"}
    )
    assert response.status_code == 422


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
