"""Admin user-management + per-user account settings (auth enabled)."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case
from app.models.enums import CaseStatus, Jurisdiction, UserRole
from app.services import auth_service


def _client():
    return TestClient(app, follow_redirects=False)


def _login(client, email, password="password123"):
    client.post("/api/v1/auth/login", json={"email": email, "password": password})


@pytest.fixture
def admin(db_session):
    u = auth_service.create_user(
        db_session,
        email="admin@example.com",
        password="password123",
        role=UserRole.ADMIN,
    )
    db_session.commit()
    return u


@pytest.fixture
def regular(db_session):
    u = auth_service.create_user(
        db_session, email="reg@example.com", password="password123", role=UserRole.USER
    )
    db_session.commit()
    return u


# --- admin guard -----------------------------------------------------------


def test_non_admin_blocked_from_admin_users(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    assert client.get("/admin/users").status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 403


def test_admin_can_list_users(auth_enabled, db_session, admin):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.get("/api/v1/admin/users")
    assert resp.status_code == 200
    body = resp.json()
    assert {u["email"] for u in body["users"]} >= {"admin@example.com"}
    assert body["signup_enabled"] is False
    assert client.get("/admin/users").status_code == 200  # SPA page


def test_admin_creates_user(auth_enabled, db_session, admin):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.post(
        "/api/v1/admin/users",
        json={
            "email": "new@example.com",
            "password": "password123",
            "role": "user",
        },  # pragma: allowlist secret
    )
    assert resp.status_code == 201
    assert "new@example.com" in {u["email"] for u in resp.json()["users"]}
    assert auth_service.get_user_by_email(db_session, "new@example.com") is not None


def test_admin_toggle_active_revokes_sessions(auth_enabled, db_session, admin, regular):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.put(
        f"/api/v1/admin/users/{regular.id}/active", json={"is_active": False}
    )
    assert resp.status_code == 200
    db_session.refresh(regular)
    assert regular.is_active is False
    assert regular.token_version == 1


def test_admin_cannot_delete_self(auth_enabled, db_session, admin):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.delete(f"/api/v1/admin/users/{admin.id}")
    assert resp.status_code == 400
    assert resp.json()["code"] == "self_change"


def test_delete_blocked_when_user_owns_cases(auth_enabled, db_session, admin, regular):
    db_session.add(
        Case(
            id="OWN-1",
            title="Owned",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
            owner_id=regular.id,
        )
    )
    db_session.commit()
    client = _client()
    _login(client, "admin@example.com")
    resp = client.delete(f"/api/v1/admin/users/{regular.id}")
    assert resp.status_code == 409
    assert resp.json()["code"] == "owns_cases"


def test_signup_toggle(auth_enabled, db_session, admin):
    client = _client()
    _login(client, "admin@example.com")
    assert auth_service.signup_enabled(db_session) is False
    assert client.put("/api/v1/admin/signup", json={"enabled": True}).json()[
        "signup_enabled"
    ]
    assert auth_service.signup_enabled(db_session) is True


def test_admin_role_change_and_password_reset(auth_enabled, db_session, admin, regular):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.put(f"/api/v1/admin/users/{regular.id}/role", json={"role": "admin"})
    assert resp.status_code == 200
    db_session.refresh(regular)
    assert regular.role == UserRole.ADMIN
    assert (
        client.put(
            f"/api/v1/admin/users/{admin.id}/role", json={"role": "user"}
        ).status_code
        == 400
    )

    resp = client.put(
        f"/api/v1/admin/users/{regular.id}/password",
        json={"new_password": "freshpassword1"},  # pragma: allowlist secret
    )
    assert resp.status_code == 204
    fresh = _client()
    _login(
        fresh,
        "reg@example.com",
        password="freshpassword1",  # pragma: allowlist secret
    )  # pragma: allowlist secret
    assert fresh.get("/api/v1/shell").status_code == 200


# --- triage is per-user (any authenticated user has their own inbox) --------


def test_triage_accessible_to_regular_user(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    assert client.get("/triage").status_code == 200


# --- account self-service --------------------------------------------------


def test_change_password_wrong_current(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.put(
        "/api/v1/settings/account/password",
        json={
            "current_password": "wrong",
            "new_password": "newpassword123",
        },  # pragma: allowlist secret
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "wrong_password"


def test_change_password_success_keeps_session(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.put(
        "/api/v1/settings/account/password",
        json={
            "current_password": "password123",
            "new_password": "newpassword123",
        },  # pragma: allowlist secret
    )
    assert resp.status_code == 204
    # Session was re-issued with the new token_version → still authenticated.
    assert client.get("/").status_code == 200
    # New password works on a fresh login.
    db_session.expire_all()
    fresh = _client()
    _login(fresh, "reg@example.com", password="newpassword123")
    assert fresh.get("/").status_code == 200


def test_update_display_name(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.put(
        "/api/v1/settings/account/profile", json={"display_name": "Reggie"}
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Reggie"
    db_session.refresh(regular)
    assert regular.display_name == "Reggie"


# --- account email change --------------------------------------------------


def test_change_email_wrong_current_password(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.put(
        "/api/v1/settings/account/email",
        json={"new_email": "newreg@example.com", "current_password": "wrong"},
    )
    assert resp.status_code == 422
    db_session.refresh(regular)
    assert regular.email == "reg@example.com"


def test_change_email_invalid_format(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.put(
        "/api/v1/settings/account/email",
        json={"new_email": "notanemail", "current_password": "password123"},
    )
    assert resp.status_code == 422


def test_change_email_duplicate(auth_enabled, db_session, admin, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.put(
        "/api/v1/settings/account/email",
        json={"new_email": "admin@example.com", "current_password": "password123"},
    )
    assert resp.status_code == 422


def test_change_email_success_keeps_session_and_login(
    auth_enabled, db_session, regular
):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.put(
        "/api/v1/settings/account/email",
        json={"new_email": "Renamed@Example.com", "current_password": "password123"},
    )
    assert resp.status_code == 200
    db_session.refresh(regular)
    assert regular.email == "renamed@example.com"  # normalized
    # Session keys on uid, so the current session stays authenticated.
    assert client.get("/").status_code == 200
    # Old email no longer resolves; new email maps to the same user row.
    assert auth_service.get_user_by_email(db_session, "reg@example.com") is None
    assert (
        auth_service.get_user_by_email(db_session, "renamed@example.com").id
        == regular.id
    )
    # Fresh login works with the new email.
    fresh = _client()
    _login(fresh, "renamed@example.com")
    assert fresh.get("/").status_code == 200


def test_change_email_preserves_document_ownership(auth_enabled, db_session, regular):
    rid = regular.id
    db_session.add(
        Case(
            id="OWN-EMAIL",
            title="Owned",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
            owner_id=rid,
        )
    )
    db_session.commit()
    auth_service.change_email(db_session, regular, "moved@example.com")
    db_session.commit()
    # Ownership FK references users.id, untouched by the email rename.
    case = db_session.get(Case, "OWN-EMAIL")
    assert case.owner_id == rid


def test_change_email_keeps_bootstrap_admin_pinned(db_session):
    """The bootstrap admin is pinned by id, so renaming its email re-resolves to
    the same row instead of spawning a fresh admin."""
    admin = auth_service.get_or_create_bootstrap_admin(db_session)
    assert admin is not None
    db_session.commit()
    before = auth_service.count_users(db_session)

    auth_service.change_email(db_session, admin, "owner@example.com")
    db_session.commit()

    again = auth_service.get_or_create_bootstrap_admin(db_session)
    db_session.commit()
    assert again.id == admin.id
    assert again.email == "owner@example.com"
    assert auth_service.count_users(db_session) == before  # no second admin created


# --- bootstrap admin resolution (id-pin, env provisioning, first-run) -------


def _wipe_users(db_session):
    from app.models.database import AppSettings, User

    db_session.query(User).delete()
    db_session.query(AppSettings).delete()
    db_session.commit()


def test_env_provisions_primary_admin_on_fresh_db(db_session, monkeypatch):
    """Fresh DB + both env creds → the code-driven (Ansible) path seeds and pins
    the primary admin."""
    import app.config as cfg

    _wipe_users(db_session)
    monkeypatch.setattr(cfg, "BOOTSTRAP_ADMIN_EMAIL", "ops@corp.com")
    monkeypatch.setattr(cfg, "BOOTSTRAP_ADMIN_PASSWORD", "ansible-secret-1")

    admin = auth_service.get_or_create_bootstrap_admin(db_session)
    db_session.commit()
    assert admin is not None
    assert admin.email == "ops@corp.com"
    assert admin.role == UserRole.ADMIN
    assert auth_service.bootstrap_admin_id(db_session) == admin.id


def test_env_email_without_password_does_not_provision(db_session, monkeypatch):
    """Half-configured env (email but no password) must NOT seed — the create-admin
    screen onboards instead."""
    import app.config as cfg

    _wipe_users(db_session)
    monkeypatch.setattr(cfg, "BOOTSTRAP_ADMIN_EMAIL", "ops@corp.com")
    monkeypatch.setattr(cfg, "BOOTSTRAP_ADMIN_PASSWORD", "")

    assert auth_service.get_or_create_bootstrap_admin(db_session) is None
    assert auth_service.count_users(db_session) == 0


def test_existing_admin_is_backfilled_and_pinned(db_session, monkeypatch):
    """An admin created without an explicit pin (e.g. via the first-run screen, or
    on upgrade) gets resolved and pinned by id."""
    import app.config as cfg

    _wipe_users(db_session)
    monkeypatch.setattr(cfg, "BOOTSTRAP_ADMIN_EMAIL", "")
    monkeypatch.setattr(cfg, "BOOTSTRAP_ADMIN_PASSWORD", "")
    created = auth_service.create_user(
        db_session,
        email="founder@example.com",
        password="password123",
        role=UserRole.ADMIN,
    )
    db_session.commit()

    resolved = auth_service.get_or_create_bootstrap_admin(db_session)
    db_session.commit()
    assert resolved is not None and resolved.id == created.id
    assert auth_service.bootstrap_admin_id(db_session) == created.id


def test_dev_mode_fresh_db_redirects_to_create_admin(db_session):
    """AUTH_ENABLED=false on a fresh DB has no one to auto-bind, so a protected
    page sends the user to the one-time create-admin screen."""
    _wipe_users(db_session)
    client = _client()
    resp = client.get("/triage")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/signup"


def test_admin_active_flag_is_idempotent_and_self_guarded(
    auth_enabled, db_session, admin, regular
):
    client = _client()
    _login(client, "admin@example.com")
    for _ in range(2):
        resp = client.put(
            f"/api/v1/admin/users/{regular.id}/active", json={"is_active": False}
        )
        assert resp.status_code == 200
    db_session.refresh(regular)
    assert regular.is_active is False
    assert regular.token_version == 1  # bumped once, not twice
    self_resp = client.put(
        f"/api/v1/admin/users/{admin.id}/active", json={"is_active": False}
    )
    assert self_resp.status_code == 400
    assert self_resp.json()["code"] == "self_change"
    assert (
        client.put(
            f"/api/v1/admin/users/{admin.id}/password",
            json={"new_password": "freshpassword1"},  # pragma: allowlist secret
        ).status_code
        == 400
    )


def test_admin_reassign_cases_validation(auth_enabled, db_session, admin, regular):
    client = _client()
    _login(client, "admin@example.com")
    same = client.post(
        f"/api/v1/admin/users/{regular.id}/reassign-cases",
        json={"new_owner_id": regular.id},
    )
    assert same.status_code == 400
    assert same.json()["code"] == "same_owner"
    missing = client.post(
        f"/api/v1/admin/users/{regular.id}/reassign-cases",
        json={"new_owner_id": 999999},
    )
    assert missing.status_code == 404


def test_change_email_without_password_needs_no_current_password(
    auth_enabled, db_session
):
    user = auth_service.create_user(db_session, email="sso@example.com", password=None)
    db_session.commit()
    # No password hash means no local login; drive the request via a session cookie.
    import app.main as main_module
    from app.services.auth_service import build_session

    client = _client()
    client.cookies.set("session", main_module._dump_session_cookie(build_session(user)))
    resp = client.put(
        "/api/v1/settings/account/email", json={"new_email": "sso2@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "sso2@example.com"
    assert resp.json()["has_password"] is False
