"""OIDC / authentik (Phase 2): account linking + route behaviour."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.enums import UserRole
from app.services import auth_service

ISS = "https://idp.example.com"


# --- account linking (service layer) ---------------------------------------


def test_link_by_subject(db_session):
    u = auth_service.create_user(
        db_session, email="x@example.com", password="password123"
    )
    u.oidc_issuer = ISS
    u.oidc_subject = "sub-1"
    db_session.commit()

    got = auth_service.link_or_create_oidc_user(
        db_session,
        issuer=ISS,
        subject="sub-1",
        email=None,
        display_name=None,
        signup_allowed=False,
    )
    assert got is not None and got.id == u.id


def test_link_by_email_sets_subject(db_session):
    u = auth_service.create_user(
        db_session, email="y@example.com", password="password123"
    )
    db_session.commit()

    got = auth_service.link_or_create_oidc_user(
        db_session,
        issuer=ISS,
        subject="sub-2",
        email="y@example.com",
        display_name="Y",
        signup_allowed=False,
    )
    assert got is not None and got.id == u.id
    db_session.refresh(u)
    assert u.oidc_subject == "sub-2"
    assert u.oidc_issuer == ISS


def test_create_when_signup_allowed(db_session):
    got = auth_service.link_or_create_oidc_user(
        db_session,
        issuer=ISS,
        subject="sub-3",
        email="new@example.com",
        display_name="New",
        signup_allowed=True,
    )
    assert got is not None
    assert got.role == UserRole.USER
    assert got.password_hash is None  # OIDC-only account


def test_no_account_when_signup_disabled(db_session):
    got = auth_service.link_or_create_oidc_user(
        db_session,
        issuer=ISS,
        subject="sub-4",
        email="nope@example.com",
        display_name=None,
        signup_allowed=False,
    )
    assert got is None


def test_inactive_linked_user_rejected(db_session):
    u = auth_service.create_user(
        db_session, email="z@example.com", password="password123"
    )
    u.oidc_issuer = ISS
    u.oidc_subject = "sub-5"
    u.is_active = False
    db_session.commit()
    got = auth_service.link_or_create_oidc_user(
        db_session,
        issuer=ISS,
        subject="sub-5",
        email=None,
        display_name=None,
        signup_allowed=True,
    )
    assert got is None


# --- routes ----------------------------------------------------------------


def test_oidc_login_404_when_disabled(auth_enabled, db_session):
    # OIDC unconfigured by default → routes 404.
    client = TestClient(app, follow_redirects=False)
    assert client.get("/auth/oidc/login").status_code == 404


def _enable_oidc(monkeypatch):
    import app.config as cfg

    monkeypatch.setattr(cfg, "OIDC_ISSUER", ISS)
    monkeypatch.setattr(cfg, "OIDC_CLIENT_ID", "client")
    monkeypatch.setattr(cfg, "OIDC_CLIENT_SECRET", "secret")


def test_oidc_callback_logs_in_linked_user(auth_enabled, db_session, monkeypatch):
    _enable_oidc(monkeypatch)
    u = auth_service.create_user(
        db_session, email="sso@example.com", password="password123"
    )
    u.oidc_issuer = ISS
    u.oidc_subject = "sub-sso"
    db_session.commit()

    class _FakeClient:
        async def authorize_access_token(self, request):
            return {
                "userinfo": {
                    "sub": "sub-sso",
                    "email": "sso@example.com",
                    "name": "SSO User",
                }
            }

    from app.api import auth_oidc

    monkeypatch.setattr(auth_oidc, "_get_client", lambda: _FakeClient())

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/auth/oidc/callback?code=abc&state=xyz")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    # Session established → protected page reachable.
    assert client.get("/").status_code == 200


def test_oidc_callback_rejects_unknown_when_signup_off(
    auth_enabled, db_session, monkeypatch
):
    _enable_oidc(monkeypatch)

    class _FakeClient:
        async def authorize_access_token(self, request):
            return {"userinfo": {"sub": "ghost", "email": "ghost@example.com"}}

    from app.api import auth_oidc

    monkeypatch.setattr(auth_oidc, "_get_client", lambda: _FakeClient())

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/auth/oidc/callback?code=abc&state=xyz")
    assert resp.status_code == 401
    assert b"No account" in resp.content


@pytest.fixture(autouse=True)
def _reset_oidc_client():
    from app.api import auth_oidc

    auth_oidc.reset_client()
    yield
    auth_oidc.reset_client()
