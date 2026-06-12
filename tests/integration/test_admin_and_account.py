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
    client.post("/login", data={"email": email, "password": password})


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


def test_admin_can_list_users(auth_enabled, db_session, admin):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert b"Manage Users" in resp.content


def test_admin_creates_user(auth_enabled, db_session, admin):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.post(
        "/admin/users",
        data={"email": "new@example.com", "password": "password123", "role": "user"},
    )
    assert resp.status_code == 204
    assert auth_service.get_user_by_email(db_session, "new@example.com") is not None


def test_admin_toggle_active_revokes_sessions(auth_enabled, db_session, admin, regular):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.post(f"/admin/users/{regular.id}/toggle-active")
    assert resp.status_code == 204
    db_session.refresh(regular)
    assert regular.is_active is False
    assert regular.token_version == 1


def test_admin_cannot_delete_self(auth_enabled, db_session, admin):
    client = _client()
    _login(client, "admin@example.com")
    resp = client.post(f"/admin/users/{admin.id}/delete")
    assert resp.status_code == 400


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
    resp = client.post(f"/admin/users/{regular.id}/delete")
    assert resp.status_code == 409


def test_signup_toggle(auth_enabled, db_session, admin):
    client = _client()
    _login(client, "admin@example.com")
    assert auth_service.signup_enabled(db_session) is False
    client.post("/admin/signup-toggle")
    assert auth_service.signup_enabled(db_session) is True


# --- triage is per-user (any authenticated user has their own inbox) --------


def test_triage_accessible_to_regular_user(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    assert client.get("/triage").status_code == 200


# --- account self-service --------------------------------------------------


def test_change_password_wrong_current(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.post(
        "/api/settings/account/password",
        data={"current_password": "wrong", "new_password": "newpassword123"},
    )
    assert resp.status_code == 422


def test_change_password_success_keeps_session(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.post(
        "/api/settings/account/password",
        data={"current_password": "password123", "new_password": "newpassword123"},
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
    resp = client.post("/api/settings/account/profile", data={"display_name": "Reggie"})
    assert resp.status_code == 204
    db_session.refresh(regular)
    assert regular.display_name == "Reggie"


# --- account email change --------------------------------------------------


def test_change_email_wrong_current_password(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.post(
        "/api/settings/account/email",
        data={"new_email": "newreg@example.com", "current_password": "wrong"},
    )
    assert resp.status_code == 422
    db_session.refresh(regular)
    assert regular.email == "reg@example.com"


def test_change_email_invalid_format(auth_enabled, db_session, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.post(
        "/api/settings/account/email",
        data={"new_email": "notanemail", "current_password": "password123"},
    )
    assert resp.status_code == 422


def test_change_email_duplicate(auth_enabled, db_session, admin, regular):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.post(
        "/api/settings/account/email",
        data={"new_email": "admin@example.com", "current_password": "password123"},
    )
    assert resp.status_code == 422


def test_change_email_success_keeps_session_and_login(
    auth_enabled, db_session, regular
):
    client = _client()
    _login(client, "reg@example.com")
    resp = client.post(
        "/api/settings/account/email",
        data={"new_email": "Renamed@Example.com", "current_password": "password123"},
    )
    assert resp.status_code == 204
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


def test_change_email_dev_mode_follows_bootstrap_admin(db_session):
    """Auth-disabled resolution must follow the renamed bootstrap admin instead
    of spawning a fresh admin@localhost."""
    admin = auth_service.get_or_create_bootstrap_admin(db_session)
    db_session.commit()
    before = auth_service.count_users(db_session)

    auth_service.change_email(db_session, admin, "owner@example.com")
    db_session.commit()

    again = auth_service.get_or_create_bootstrap_admin(db_session)
    db_session.commit()
    assert again.id == admin.id
    assert again.email == "owner@example.com"
    assert auth_service.count_users(db_session) == before  # no second admin created
