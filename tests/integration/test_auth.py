"""Authentication: gate behaviour, login/logout, signup, session revocation."""

from fastapi.testclient import TestClient

from app.core import security
from app.main import app
from app.models.enums import UserRole
from app.services import auth_service


def _client() -> TestClient:
    # follow_redirects=False so we can assert the 303 → /login behaviour.
    return TestClient(app, follow_redirects=False)


# --- password hashing ------------------------------------------------------


def test_password_hash_roundtrip():
    h = security.hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert security.verify_password("correct horse battery staple", h)
    assert not security.verify_password("wrong", h)


def test_verify_password_handles_missing_hash():
    assert security.verify_password("anything", None) is False


# --- gate ------------------------------------------------------------------


def test_dev_mode_allows_access(db_session):
    """Default fixture sets AUTH_ENABLED=false → no gating."""
    client = _client()
    resp = client.get("/")
    assert resp.status_code == 200


def test_protected_route_redirects_to_login(auth_enabled, db_session):
    client = _client()
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_htmx_request_gets_hx_redirect(auth_enabled, db_session):
    client = _client()
    resp = client.get("/", headers={"HX-Request": "true"})
    assert resp.status_code == 401
    assert resp.headers.get("HX-Redirect") == "/login"


def test_api_request_gets_401_json(auth_enabled, db_session):
    client = _client()
    resp = client.get(
        "/api/user-settings/active-proceeding/x", headers={"accept": "application/json"}
    )
    # The gate denies before routing; either 401 (gate) is what we assert.
    assert resp.status_code == 401


def test_login_page_is_public(auth_enabled, db_session):
    # Need at least one user so /login doesn't redirect to first-run signup.
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    client = _client()
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"Sign in" in resp.content


# --- login -----------------------------------------------------------------


def test_login_wrong_password_is_generic_401(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    client = _client()
    resp = client.post("/login", data={"email": "u@example.com", "password": "nope"})
    assert resp.status_code == 401
    assert b"Invalid email or password" in resp.content


def test_login_success_grants_access(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    client = _client()
    resp = client.post(
        "/login", data={"email": "u@example.com", "password": "password123"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    # Cookie now set on the client; a protected page is reachable.
    home = client.get("/")
    assert home.status_code == 200


def test_token_version_bump_revokes_session(auth_enabled, db_session):
    user = auth_service.create_user(
        db_session, email="u@example.com", password="password123"
    )
    db_session.commit()
    client = _client()
    client.post("/login", data={"email": "u@example.com", "password": "password123"})
    assert client.get("/").status_code == 200

    # Simulate admin disabling/resetting: bump token_version.
    auth_service.bump_token_version(user)
    db_session.commit()
    assert client.get("/").status_code == 303


def test_logout_clears_session(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    client = _client()
    client.post("/login", data={"email": "u@example.com", "password": "password123"})
    assert client.get("/").status_code == 200
    client.post("/logout")
    assert client.get("/").status_code == 303


# --- signup ----------------------------------------------------------------


def test_first_run_signup_creates_admin(auth_enabled, db_session):
    # Simulate a truly fresh install: the conftest seeds a dev-mode bootstrap
    # admin, so clear users first to exercise the zero-users first-run path.
    from app.models.database import User

    db_session.query(User).delete()
    db_session.commit()
    assert auth_service.count_users(db_session) == 0
    client = _client()
    page = client.get("/signup")
    assert page.status_code == 200
    resp = client.post(
        "/signup",
        data={
            "email": "boss@example.com",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert resp.status_code == 303
    created = auth_service.get_user_by_email(db_session, "boss@example.com")
    assert created is not None
    assert created.role == UserRole.ADMIN


def test_signup_disabled_returns_404_when_not_first_run(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    # signup defaults off
    client = _client()
    assert client.get("/signup").status_code == 303  # redirect to login
    resp = client.post(
        "/signup",
        data={
            "email": "new@example.com",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert resp.status_code == 404


def test_admin_dependency_blocks_regular_user(auth_enabled, db_session):
    from fastapi import HTTPException

    from app.dependencies import get_current_admin

    user = auth_service.create_user(
        db_session, email="u@example.com", password="password123", role=UserRole.USER
    )
    db_session.commit()
    try:
        get_current_admin(user=user)
        raised = False
    except HTTPException as exc:
        raised = exc.status_code == 403
    assert raised
