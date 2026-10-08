"""Gmail connection safety + sync controls (OAuth callback, settings endpoints)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import config
from app.core.secrets import SecretsError
from app.main import app
from app.models.database import User, UserSettings
from app.services import ai_config, user_settings_service
from app.services.ingestion.gmail import READONLY_SCOPE

pytestmark = pytest.mark.integration

client = TestClient(app)

CREDS = json.dumps({"token": "t", "refresh_token": "r"})  # pragma: allowlist secret
LIVE_KEY = "sk-live-1"  # pragma: allowlist secret
NO_KEY = "not-needed"  # pragma: allowlist secret


def _admin(db) -> int:
    return db.query(User).filter_by(email="admin@localhost").one().id


def _sj(db, user_id) -> dict:
    db.expire_all()
    return db.query(UserSettings).filter_by(user_id=user_id).one().settings_json


def _connect(db, user_id, **extra):
    user_settings_service.set_gmail_credentials(
        db, user_id, credentials_json=CREDS, connected_at="2026-01-01T00:00:00+00:00"
    )
    if extra:
        data = dict(_sj(db, user_id))
        data.update(extra)
        db.query(UserSettings).filter_by(user_id=user_id).one().settings_json = data
    db.commit()


def _oauth_callback(granted_scopes, fetch_error=None):
    """Drive start -> callback with a mocked Google flow."""
    flow = MagicMock()
    flow.authorization_url.return_value = ("https://accounts.google.test/auth", None)
    flow.code_verifier = "verifier-1"
    flow.credentials.granted_scopes = granted_scopes
    flow.credentials.to_json.return_value = CREDS
    if fetch_error:
        flow.fetch_token.side_effect = fetch_error
    with (
        patch("app.api.ingestion_settings.get_oauth_flow", return_value=flow),
        patch(
            "app.api.ingestion_settings.secrets.token_urlsafe", return_value="state-1"
        ),
    ):
        client.get("/api/ingest/gmail/oauth/start", follow_redirects=False)
        return client.get(
            "/api/ingest/gmail/oauth/callback?code=c&state=state-1",
            follow_redirects=False,
        )


# --- OAuth callback ----------------------------------------------------------


def test_start_does_not_request_previously_granted_scopes():
    flow = MagicMock()
    flow.authorization_url.return_value = ("https://accounts.google.test/auth", None)
    flow.code_verifier = "verifier-1"
    with patch("app.api.ingestion_settings.get_oauth_flow", return_value=flow):
        client.get("/api/ingest/gmail/oauth/start", follow_redirects=False)
    assert "include_granted_scopes" not in flow.authorization_url.call_args.kwargs


def test_callback_rejects_broader_than_readonly_grant(db_session):
    response = _oauth_callback([READONLY_SCOPE, "https://mail.google.com/"])
    assert response.status_code == 400
    assert "beyond read-only" in response.json()["detail"]
    assert not _sj(db_session, _admin(db_session)).get("gmail_credentials_json")


def test_callback_rejects_scope_change_reported_by_oauthlib(db_session):
    response = _oauth_callback(
        [READONLY_SCOPE], fetch_error=Warning("Scope has changed from a to b")
    )
    assert response.status_code == 400
    assert not _sj(db_session, _admin(db_session)).get("gmail_credentials_json")


def test_callback_stores_encrypted_and_anchors_the_watermark(db_session):
    response = _oauth_callback([READONLY_SCOPE])
    assert response.status_code in (302, 303, 307)
    sj = _sj(db_session, _admin(db_session))
    stored = sj["gmail_credentials_json"]
    assert stored.startswith("enc:v1:")
    assert "refresh_token" not in stored
    assert user_settings_service.decrypt_gmail_credentials(stored) == CREDS
    # No watermark would mean the first poll searches the senders' whole history.
    assert sj["gmail_last_sync_at"] == sj["gmail_connected_at"]


def test_callback_without_encryption_key_is_a_clear_503(db_session, monkeypatch):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    response = _oauth_callback([READONLY_SCOPE])
    assert response.status_code == 503
    assert "SECRETS_ENCRYPTION_KEY" in response.json()["detail"]


def test_reconnect_keeps_the_existing_watermark(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid, gmail_last_sync_at="2026-03-03T00:00:00+00:00")
    user_settings_service.set_gmail_credentials(
        db_session,
        uid,
        credentials_json=CREDS,
        connected_at="2026-06-06T00:00:00+00:00",
    )
    db_session.commit()
    assert _sj(db_session, uid)["gmail_last_sync_at"] == "2026-03-03T00:00:00+00:00"


# --- Settings endpoints ------------------------------------------------------


def test_view_defaults_auto_sync_off(db_session):
    body = client.get("/api/v1/settings/gmail").json()
    assert body["auto_sync"] is False
    assert body["reconnect_required"] is False
    assert body["failed_count"] == 0
    assert body["last_sync_error"] is None
    assert body["ai_external"] is False  # default endpoint is local Ollama


def test_controls_require_a_connection(db_session):
    assert (
        client.put(
            "/api/v1/settings/gmail/auto-sync", json={"enabled": True}
        ).status_code
        == 409
    )
    assert client.post("/api/v1/settings/gmail/sync").status_code == 409
    assert client.post("/api/v1/settings/gmail/reset-sync", json={}).status_code == 409


def test_auto_sync_toggle_gates_the_beat_fan_out(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    assert user_settings_service.user_ids_with_gmail_auto_sync(db_session) == []

    body = client.put("/api/v1/settings/gmail/auto-sync", json={"enabled": True}).json()
    assert body["auto_sync"] is True
    assert user_settings_service.user_ids_with_gmail_auto_sync(db_session) == [uid]

    client.put("/api/v1/settings/gmail/auto-sync", json={"enabled": False})
    assert user_settings_service.user_ids_with_gmail_auto_sync(db_session) == []


def test_sync_now_runs_even_with_auto_sync_off(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        response = client.post("/api/v1/settings/gmail/sync")
    assert response.status_code == 202
    assert dispatch.call_args.args[1] == uid


def test_reset_sync_moves_watermark_and_clears_failures(db_session):
    uid = _admin(db_session)
    _connect(
        db_session,
        uid,
        gmail_failed_message_ids=["a", "b"],
        gmail_last_sync_error="boom",
        gmail_reconnect_required=True,
    )
    body = client.post(
        "/api/v1/settings/gmail/reset-sync", json={"since": "2026-02-01"}
    ).json()
    assert body["failed_count"] == 0
    assert body["last_sync_error"] is None
    assert body["reconnect_required"] is False
    assert body["last_sync_at"].startswith("2026-02-01")

    # Omitted date = now; the watermark is never cleared.
    body = client.post("/api/v1/settings/gmail/reset-sync", json={}).json()
    assert body["last_sync_at"] and not body["last_sync_at"].startswith("2026-02-01")


def test_disconnect_revokes_and_forgets_but_keeps_filters(db_session):
    uid = _admin(db_session)
    user_settings_service.set_gmail_inbox_filters(
        db_session, uid, allowlist=["lawyer@example.com"], label_filter="Sanctuary"
    )
    _connect(db_session, uid, gmail_auto_sync=True, gmail_failed_message_ids=["a"])

    with patch("app.api.v1.settings_gmail.revoke_token") as revoke:
        body = client.delete("/api/v1/settings/gmail").json()

    revoke.assert_called_once_with(CREDS)  # revoked with the decrypted grant
    assert body["connected"] is False
    assert body["auto_sync"] is False
    assert body["last_sync_at"] is None
    assert body["allowlist"] == ["lawyer@example.com"]
    sj = _sj(db_session, uid)
    for key in (
        "gmail_credentials_json",
        "gmail_last_sync_at",
        "gmail_failed_message_ids",
    ):
        assert key not in sj


def test_disconnect_still_works_when_credentials_are_undecryptable(
    db_session, monkeypatch
):
    uid = _admin(db_session)
    _connect(db_session, uid)
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with patch("app.api.v1.settings_gmail.revoke_token") as revoke:
        body = client.delete("/api/v1/settings/gmail").json()
    revoke.assert_not_called()
    assert body["connected"] is False


# --- Wipe + AI keys ----------------------------------------------------------


def test_clear_all_data_resets_tracked_failures_but_keeps_the_connection(db_session):
    from app.services.maintenance_service import clear_all_data

    uid = _admin(db_session)
    _connect(db_session, uid, gmail_failed_message_ids=["a"])
    clear_all_data(db_session)
    sj = _sj(db_session, uid)
    assert sj["gmail_failed_message_ids"] == []
    assert sj["gmail_credentials_json"]
    assert sj["gmail_last_sync_at"]


def test_ai_key_is_encrypted_at_rest_and_wrong_key_fails_loudly(
    db_session, monkeypatch
):
    ai_config.save_instance(
        db_session,
        {
            "id": "inst_1",
            "label": "Box",
            "base_url": "http://x",
            "api_key": LIVE_KEY,
        },
    )
    raw = ai_config._raw_ai_section(db_session)["instances"][0]["api_key"]
    assert raw.startswith("enc:v1:") and LIVE_KEY not in raw
    assert ai_config.get_instance(db_session, "inst_1")["api_key"] == LIVE_KEY

    # Not silently swallowed into "no AI config" / env-default fallback.
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with pytest.raises(SecretsError):
        ai_config.get_instance(db_session, "inst_1")


def test_keyless_ai_endpoint_is_stored_as_is(db_session):
    ai_config.save_instance(
        db_session,
        {
            "id": "inst_2",
            "label": "Local",
            "base_url": "http://x",
            "api_key": NO_KEY,
        },
    )
    assert ai_config._raw_ai_section(db_session)["instances"][0]["api_key"] == NO_KEY


def test_view_flags_an_external_ai_endpoint(db_session):
    ai_config.save_instance(
        db_session,
        {
            "id": "inst_ext",
            "label": "Cloud",
            "base_url": "https://api.openai.com/v1",
            "api_key": LIVE_KEY,
        },
    )
    ai_config.set_active(db_session, "chat", "inst_ext")
    assert client.get("/api/v1/settings/gmail").json()["ai_external"] is True


def test_fan_out_skips_users_whose_grant_needs_a_reconnect(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid, gmail_auto_sync=True, gmail_reconnect_required=True)
    assert user_settings_service.user_ids_with_gmail_auto_sync(db_session) == []
    # Reconnecting clears the flag, so polling resumes.
    user_settings_service.set_gmail_credentials(
        db_session,
        uid,
        credentials_json=CREDS,
        connected_at="2026-02-02T00:00:00+00:00",
    )
    db_session.commit()
    assert user_settings_service.user_ids_with_gmail_auto_sync(db_session) == [uid]


def test_missing_encryption_key_is_a_clear_503_on_the_ai_routes(
    db_session, monkeypatch
):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    response = client.post(
        "/api/v1/settings/ai/instances",
        json={"label": "Cloud", "base_url": "http://x", "api_key": LIVE_KEY},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "secrets_key_unavailable"
    assert "SECRETS_ENCRYPTION_KEY" in response.json()["detail"]


def test_reset_sync_refuses_a_date_in_the_future(db_session):
    from datetime import UTC, datetime, timedelta

    _connect(db_session, _admin(db_session))
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    today = datetime.now(UTC).date().isoformat()

    refused = client.post("/api/v1/settings/gmail/reset-sync", json={"since": tomorrow})
    assert refused.status_code == 422
    assert "future" in refused.json()["detail"]
    assert (
        client.post(
            "/api/v1/settings/gmail/reset-sync", json={"since": today}
        ).status_code
        == 200
    )


# --- PKCE: the verifier made at /start must reach /callback -------------------


def test_callback_sends_the_verifier_made_at_start(db_session):
    flow = MagicMock()
    flow.authorization_url.return_value = ("https://accounts.google.test/auth", None)
    flow.code_verifier = "verifier-1"
    flow.credentials.granted_scopes = [READONLY_SCOPE]
    flow.credentials.to_json.return_value = CREDS
    with (
        patch("app.api.ingestion_settings.get_oauth_flow", return_value=flow),
        patch("app.api.ingestion_settings.secrets.token_urlsafe", return_value="s"),
    ):
        client.get("/api/ingest/gmail/oauth/start", follow_redirects=False)
        client.get(
            "/api/ingest/gmail/oauth/callback?code=c&state=s", follow_redirects=False
        )
    flow.fetch_token.assert_called_once_with(code="c", code_verifier="verifier-1")


def test_start_and_callback_agree_on_pkce_with_a_real_google_flow(db_session):
    """Regression: /start and /callback each build their own Flow. google-auth-
    oauthlib generates the PKCE verifier on the first one, so unless it is carried
    over the second one sends none and Google answers `invalid_grant: Missing code
    verifier` (what the first real Connect Gmail hit). No mocking of the Flow
    itself: the verifier sent at the end must hash to the challenge in the URL."""
    import base64
    import hashlib
    import time
    from urllib.parse import parse_qs, urlparse

    from google_auth_oauthlib.flow import Flow

    seen: dict = {}

    class _Flow(Flow):
        def fetch_token(self, **kwargs):
            seen.update(kwargs)
            self.oauth2session.token = {
                "access_token": "t",
                "refresh_token": "r",
                "scope": [READONLY_SCOPE],
                "token_type": "Bearer",
                "expires_in": 3600,
                "expires_at": time.time() + 3600,
            }

    def _make():
        return _Flow.from_client_config(
            {
                "web": {
                    "client_id": "id.apps.googleusercontent.com",
                    "client_secret": "s",  # pragma: allowlist secret
                    "auth_uri": "https://accounts.google.test/auth",
                    "token_uri": "https://oauth2.google.test/token",
                }
            },
            scopes=[READONLY_SCOPE],
            redirect_uri="https://sanctuary.test/api/ingest/gmail/oauth/callback",
        )

    with (
        patch("app.api.ingestion_settings.get_oauth_flow", side_effect=_make),
        patch("app.api.ingestion_settings.secrets.token_urlsafe", return_value="s1"),
    ):
        start = client.get("/api/ingest/gmail/oauth/start", follow_redirects=False)
        challenge = parse_qs(urlparse(start.headers["location"]).query)[
            "code_challenge"
        ][0]
        done = client.get(
            "/api/ingest/gmail/oauth/callback?code=c&state=s1", follow_redirects=False
        )

    assert done.status_code in (302, 303, 307), done.text
    verifier = seen["code_verifier"]
    assert verifier, "no code_verifier reached the token exchange"
    digest = hashlib.sha256(verifier.encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge


def test_google_rejecting_the_code_is_a_readable_400_not_a_bare_bad_request(db_session):
    from oauthlib.oauth2.rfc6749.errors import InvalidGrantError

    response = _oauth_callback(
        [READONLY_SCOPE],
        fetch_error=InvalidGrantError(description="Missing code verifier."),
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "invalid_grant" in detail and "Start again" in detail
    assert not _sj(db_session, _admin(db_session)).get("gmail_credentials_json")


# --- Filters: a sender or a label is required -------------------------------


@pytest.mark.parametrize(
    "body,fragment",
    [
        ({"allowlist": [], "label_filter": ""}, "allowlist or a label"),
        ({"allowlist": ["  "], "label_filter": "  "}, "allowlist or a label"),
        ({"allowlist": ["not an address"]}, "isn't an email address or domain"),
        ({"allowlist": ["a@b@c.de"]}, "isn't an email address or domain"),
        ({"allowlist": ["from:foo@bar.de"]}, "isn't an email address or domain"),
        ({"allowlist": ["to:me@bar.de"]}, "isn't an email address or domain"),
        ({"allowlist": ["in:anywhere"]}, "isn't an email address or domain"),
        ({"allowlist": ["a@bar.de OR b@bar.de"]}, "isn't an email address or domain"),
        ({"allowlist": ["bar.de) OR (from:x"]}, "isn't an email address or domain"),
        ({"allowlist": [], "label_filter": "“Sanctuary”"}, "quotes"),
        ({"allowlist": [], "label_filter": 'x" OR in:anywhere'}, "quotes"),
    ],
)
def test_filters_reject_unbounded_or_malformed_input(db_session, body, fragment):
    resp = client.put("/api/v1/settings/gmail/filters", json=body)
    assert resp.status_code == 422
    assert fragment in resp.text


@pytest.mark.parametrize(
    "body",
    [
        {"allowlist": ["info@haidlfunk.de"]},
        {"allowlist": ["haidlfunk.de", "@firm.de"], "label_filter": "Sanctuary"},
        {"allowlist": [], "label_filter": "Sanctuary"},
    ],
)
def test_filters_accept_a_sender_a_label_or_both(db_session, body):
    resp = client.put("/api/v1/settings/gmail/filters", json=body)
    assert resp.status_code == 200
    assert resp.json()["allowlist"] == body["allowlist"]


def test_filter_preview_counts_matches_without_saving(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    service = MagicMock()
    listing = service.users.return_value.messages.return_value.list
    listing.return_value.execute.return_value = {"resultSizeEstimate": 214}

    with patch("app.api.v1.settings_gmail.connect_gmail", return_value=service):
        resp = client.post(
            "/api/v1/settings/gmail/filters/preview",
            json={"allowlist": [], "label_filter": "Sanctuary"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"estimate": 214}
    listing.assert_called_once_with(userId="me", q='label:"Sanctuary"', maxResults=1)
    assert "gmail_label_filter" not in _sj(db_session, uid) or (
        _sj(db_session, uid)["gmail_label_filter"] != "Sanctuary"
    )


def test_filter_preview_needs_a_connection_and_a_filter(db_session):
    body = {"allowlist": ["a@firm.de"]}
    assert (
        client.post("/api/v1/settings/gmail/filters/preview", json=body).json()["code"]
        == "gmail_not_connected"
    )
    _connect(db_session, _admin(db_session))
    assert (
        client.post(
            "/api/v1/settings/gmail/filters/preview", json={"allowlist": []}
        ).status_code
        == 422
    )


def test_filter_preview_reports_a_dead_grant_and_gmail_outages(db_session):
    from app.services.ingestion.gmail import GmailReconnectRequired

    _connect(db_session, _admin(db_session))
    body = {"allowlist": ["a@firm.de"]}
    url = "/api/v1/settings/gmail/filters/preview"
    with patch(
        "app.api.v1.settings_gmail.connect_gmail",
        side_effect=GmailReconnectRequired("gone"),
    ):
        assert client.post(url, json=body).json()["code"] == "gmail_reconnect_required"
    with patch("app.api.v1.settings_gmail.connect_gmail", side_effect=OSError("down")):
        resp = client.post(url, json=body)
    assert resp.status_code == 502 and resp.json()["code"] == "gmail_unreachable"
