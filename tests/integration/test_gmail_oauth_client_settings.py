"""Settings → Gmail: the instance-wide Google OAuth client (UI over env)."""

from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.models.database import AppSettings
from app.models.enums import UserRole
from app.services import gmail_oauth_config

client = TestClient(app)

URL = "/api/v1/settings/gmail"
CLIENT_ID = "1234-abc.apps.googleusercontent.com"
PASSWORD = "password123"  # pragma: allowlist secret
BODY = {
    "client_id": CLIENT_ID,
    "client_secret": "GOCSPX-secret",  # pragma: allowlist secret
    "redirect_uri": "https://sanctuary.test/api/ingest/gmail/oauth/callback",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REDIRECT_URI"):
        monkeypatch.setattr(config, name, "")


def test_unconfigured_by_default_and_start_is_a_clean_409():
    view = client.get(URL).json()["oauth_client"]
    assert view["configured"] is False and view["source"] is None
    assert view["redirect_uri"].endswith("/api/ingest/gmail/oauth/callback")
    resp = client.get("/api/ingest/gmail/oauth/start", follow_redirects=False)
    assert resp.status_code == 409


def test_save_then_start_uses_the_stored_client_and_never_leaks_the_secret(db_session):
    resp = client.put(f"{URL}/oauth-client", json=BODY)
    assert resp.status_code == 200, resp.text
    assert "GOCSPX" not in resp.text
    view = resp.json()["oauth_client"]
    assert view["configured"] and view["source"] == "ui" and view["has_secret"]
    assert view["client_id"] == CLIENT_ID and view["can_edit"] is True

    stored = db_session.query(AppSettings).first().settings_json["gmail_oauth"]
    assert stored["client_secret"].startswith("enc:v1:")

    start = client.get("/api/ingest/gmail/oauth/start", follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [BODY["redirect_uri"]]


def test_omitting_the_secret_keeps_the_stored_one(db_session):
    client.put(f"{URL}/oauth-client", json=BODY)
    resp = client.put(
        f"{URL}/oauth-client",
        json={
            **BODY,
            "client_id": "5678-def.apps.googleusercontent.com",
            "client_secret": None,
        },
    )
    assert resp.status_code == 200
    assert (
        gmail_oauth_config.get_oauth_client(db_session).client_secret
        == BODY["client_secret"]
    )


def test_first_save_requires_a_secret():
    resp = client.put(f"{URL}/oauth-client", json={**BODY, "client_secret": None})
    assert resp.status_code == 422 and resp.json()["code"] == "client_secret_required"


@pytest.mark.parametrize(
    "patch_",
    [{"client_id": "not-a-google-id"}, {"redirect_uri": "javascript:alert(1)"}],
)
def test_rejects_malformed_values(patch_):
    assert client.put(f"{URL}/oauth-client", json={**BODY, **patch_}).status_code == 422


def test_clear_falls_back_to_env(db_session, monkeypatch):
    client.put(f"{URL}/oauth-client", json=BODY)
    monkeypatch.setattr(config, "GMAIL_CLIENT_ID", "env.apps.googleusercontent.com")
    monkeypatch.setattr(
        config, "GMAIL_CLIENT_SECRET", "env-secret"
    )  # pragma: allowlist secret
    view = client.delete(f"{URL}/oauth-client").json()["oauth_client"]
    assert (
        view["source"] == "env"
        and view["client_id"] == "env.apps.googleusercontent.com"
    )


def test_stored_values_win_field_by_field(db_session, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_CLIENT_ID", "env.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GMAIL_REDIRECT_URI", "https://env.test/cb")
    client.put(f"{URL}/oauth-client", json=BODY)
    got = gmail_oauth_config.get_oauth_client(db_session)
    assert got.client_id == CLIENT_ID and got.redirect_uri == BODY["redirect_uri"]


def test_non_admin_cannot_change_it(auth_enabled, db_session):
    from app.services import auth_service

    auth_service.create_user(
        db_session, email="root@example.com", password=PASSWORD, role=UserRole.ADMIN
    )
    auth_service.create_user(db_session, email="alice@example.com", password=PASSWORD)
    db_session.commit()
    alice = TestClient(app, follow_redirects=False)
    alice.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert alice.put(f"{URL}/oauth-client", json=BODY).status_code == 403
    assert alice.delete(f"{URL}/oauth-client").status_code == 403
    assert alice.get(URL).json()["oauth_client"]["can_edit"] is False


def test_unreadable_secret_does_not_take_the_page_down(db_session, monkeypatch):
    client.put(f"{URL}/oauth-client", json=BODY)
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    resp = client.get(URL)
    assert resp.status_code == 200
    view = resp.json()["oauth_client"]
    assert view["configured"] is False and view["has_secret"] is True
    assert view["client_id"] == CLIENT_ID and view["error"]
    # Saving without a secret still works the shape check; the stored one is kept.
    assert (
        client.put(
            f"{URL}/oauth-client", json={**BODY, "client_secret": None}
        ).status_code
        == 200
    )
