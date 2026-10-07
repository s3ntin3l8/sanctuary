"""Authentication: gate behaviour, login/logout, signup, session revocation."""

from fastapi.testclient import TestClient

from app.api.v1.auth import safe_next
from app.core import security
from app.main import app
from app.models.enums import UserRole
from app.services import auth_service


def _client() -> TestClient:
    # follow_redirects=False so we can assert the 303 → /login behaviour.
    return TestClient(app, follow_redirects=False)


# --- open-redirect guard -----------------------------------------------------


def test_safe_next_allows_relative_path():
    assert safe_next("/cases/ADV-1") == "/cases/ADV-1"


def test_safe_next_blocks_protocol_relative():
    assert safe_next("//evil.com") == "/"


def test_safe_next_blocks_backslash_protocol_relative():
    """'/\\evil.com' is browser-normalized to '//evil.com' (protocol-relative)
    even though it passes a naive startswith('/') check."""
    assert safe_next("/\\evil.com") == "/"
    assert safe_next("/\\/evil.com") == "/"


def test_safe_next_blocks_absolute_and_missing():
    assert safe_next("https://evil.com") == "/"
    assert safe_next(None) == "/"


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
    assert resp.headers["content-type"].startswith("text/html")


def test_login_page_redirects_to_first_run_signup(auth_enabled, db_session):
    from app.models.database import User

    db_session.query(User).delete()
    db_session.commit()
    resp = _client().get("/login")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/signup"


def test_spa_route_reports_missing_bundle(
    auth_enabled, db_session, monkeypatch, tmp_path
):
    import app.config as cfg

    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    monkeypatch.setattr(cfg, "FRONTEND_DIST", tmp_path / "nowhere")
    resp = _client().get("/login")
    assert resp.status_code == 503
    assert b"make frontend-build" in resp.content


def test_auth_config_is_public(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    resp = _client().get("/api/v1/auth/config")
    assert resp.status_code == 200
    assert resp.json() == {
        "first_run": False,
        "signup_enabled": False,
        "oidc_enabled": False,
        "oidc_provider_name": "authentik",
    }


# --- login -----------------------------------------------------------------


def test_login_wrong_password_is_generic_401(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    client = _client()
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "u@example.com", "password": "nope"},  # pragma: allowlist secret
    )
    assert resp.status_code == 401
    assert resp.json() == {
        "detail": "Invalid email or password.",
        "code": "invalid_credentials",
    }


def test_login_rejects_malformed_body_with_uniform_error(auth_enabled, db_session):
    resp = _client().post("/api/v1/auth/login", json={"email": "u@example.com"})
    assert resp.status_code == 422
    assert resp.json() == {
        "detail": "password: Field required",
        "code": "validation_error",
    }


def test_plain_http_exception_under_v1_is_uniform_json(auth_enabled, db_session):
    """A dependency raising a bare HTTPException (not ApiError) still gets {detail, code}."""
    from fastapi import HTTPException

    from app.main import app as _app

    @_app.get("/api/v1/_test/forbidden")
    def _forbidden():
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        auth_service.create_user(
            db_session, email="u@example.com", password="password123"
        )
        db_session.commit()
        client = _client()
        client.post(
            "/api/v1/auth/login",
            json={"email": "u@example.com", "password": "password123"},
        )
        resp = client.get("/api/v1/_test/forbidden")
    finally:
        _app.router.routes[:] = [
            r
            for r in _app.router.routes
            if getattr(r, "path", "") != "/api/v1/_test/forbidden"
        ]
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access required", "code": "forbidden"}


def test_login_rate_limit_is_uniform_json(auth_enabled, db_session):
    from app.core.rate_limit import limiter

    limiter.enabled = True
    try:
        limiter.reset()
        client = _client()
        attempt = {
            "email": "x@example.com",
            "password": "nope",  # pragma: allowlist secret
        }
        for _ in range(10):
            client.post("/api/v1/auth/login", json=attempt)
        resp = client.post("/api/v1/auth/login", json=attempt)
    finally:
        limiter.reset()
        limiter.enabled = False
    assert resp.status_code == 429
    assert resp.json()["code"] == "rate_limited"
    assert resp.json()["detail"].startswith("Too many attempts")


def test_unknown_v1_route_is_json_404(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    client = _client()
    client.post(
        "/api/v1/auth/login", json={"email": "u@example.com", "password": "password123"}
    )
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found", "code": "not_found"}


def test_unauthenticated_v1_request_is_json_401(auth_enabled, db_session):
    resp = _client().get("/api/v1/does-not-exist")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Not authenticated", "code": "not_authenticated"}


def test_login_success_grants_access(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    client = _client()
    resp = client.post(
        "/api/v1/auth/login",
        json={
            "email": "u@example.com",
            "password": "password123",
            "next": "//evil.com",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"next": "/"}
    # Cookie now set on the client; a protected page is reachable.
    home = client.get("/")
    assert home.status_code == 200


def test_token_version_bump_revokes_session(auth_enabled, db_session):
    user = auth_service.create_user(
        db_session, email="u@example.com", password="password123"
    )
    db_session.commit()
    client = _client()
    client.post(
        "/api/v1/auth/login", json={"email": "u@example.com", "password": "password123"}
    )
    assert client.get("/").status_code == 200

    # Simulate admin disabling/resetting: bump token_version.
    auth_service.bump_token_version(user)
    db_session.commit()
    assert client.get("/").status_code == 303


def test_logout_clears_session(auth_enabled, db_session):
    auth_service.create_user(db_session, email="u@example.com", password="password123")
    db_session.commit()
    client = _client()
    client.post(
        "/api/v1/auth/login", json={"email": "u@example.com", "password": "password123"}
    )
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
        "/api/v1/auth/signup",
        json={
            "email": "boss@example.com",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"next": "/"}
    assert client.get("/").status_code == 200  # signed in
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
        "/api/v1/auth/signup",
        json={
            "email": "new@example.com",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "signup_disabled"


def test_signup_validation_codes(auth_enabled, db_session):
    from app.models.database import User

    db_session.query(User).delete()
    db_session.commit()
    client = _client()

    def attempt(**overrides):
        body = {
            "email": "boss@example.com",
            "password": "password123",
            "password_confirm": "password123",
        }
        body.update(overrides)
        return client.post("/api/v1/auth/signup", json=body)

    assert attempt(email="nope").json()["code"] == "invalid_email"
    short = attempt(
        password="short",  # pragma: allowlist secret
        password_confirm="short",  # pragma: allowlist secret
    )
    assert short.json()["code"] == "password_too_short"
    mismatch = attempt(password_confirm="password124")  # pragma: allowlist secret
    assert mismatch.json()["code"] == "password_mismatch"
    assert attempt().status_code == 200
    # Second account for an existing email: generic 409, never "already registered".
    auth_service.set_signup_enabled(db_session, True)
    db_session.commit()
    again = _client().post(
        "/api/v1/auth/signup",
        json={
            "email": "boss@example.com",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert again.status_code == 409
    assert again.json()["code"] == "email_unavailable"


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
