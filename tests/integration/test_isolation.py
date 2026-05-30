"""Per-user case isolation (Phase 1): a regular user sees only their own cases."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case
from app.models.enums import CaseStatus, Jurisdiction, UserRole
from app.services import access_service, auth_service
from app.services.case_service import CaseService


def _make_case(db, case_id, owner_id):
    case = Case(
        id=case_id,
        title=f"Case {case_id}",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=owner_id,
    )
    db.add(case)
    db.commit()
    return case


@pytest.fixture
def two_users(db_session):
    a = auth_service.create_user(
        db_session, email="a@example.com", password="password123"
    )
    b = auth_service.create_user(
        db_session, email="b@example.com", password="password123"
    )
    db_session.commit()
    return a, b


# --- service layer ---------------------------------------------------------


def test_directory_shows_only_owned_cases(db_session, two_users):
    a, b = two_users
    _make_case(db_session, "ISO-A", a.id)
    _make_case(db_session, "ISO-B", b.id)

    data = CaseService(db_session).get_all_cases_directory(a.id)
    ids = {c["id"] for c in data["cases"]}
    assert "ISO-A" in ids
    assert "ISO-B" not in ids


def test_admin_sees_all_cases(db_session, two_users):
    a, b = two_users
    admin = auth_service.create_user(
        db_session,
        email="admin2@example.com",
        password="password123",
        role=UserRole.ADMIN,
    )
    db_session.commit()
    _make_case(db_session, "ISO-A", a.id)
    _make_case(db_session, "ISO-B", b.id)

    data = CaseService(db_session).get_all_cases_directory(admin.id)
    ids = {c["id"] for c in data["cases"]}
    assert {"ISO-A", "ISO-B"} <= ids


def test_can_view_case_blocks_non_owner(db_session, two_users):
    a, b = two_users
    case = _make_case(db_session, "ISO-A", a.id)
    assert access_service.can_view_case(db_session, a, case) is True
    assert access_service.can_view_case(db_session, b, case) is False


def test_visible_case_ids_admin_is_none(db_session, two_users):
    a, _ = two_users
    admin = auth_service.create_user(
        db_session,
        email="admin3@example.com",
        password="password123",
        role=UserRole.ADMIN,
    )
    db_session.commit()
    assert access_service.visible_case_ids(db_session, admin) is None
    assert access_service.visible_case_ids(db_session, a) == set()


# --- route layer (auth enabled, real login) --------------------------------


def test_case_detail_404_for_non_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "ISO-A", a.id)

    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"email": "b@example.com", "password": "password123"})
    # B is logged in; A owns ISO-A → B must get 404.
    resp = client.get("/cases/ISO-A")
    assert resp.status_code == 404


def test_case_detail_ok_for_owner(auth_enabled, db_session, two_users):
    a, b = two_users
    _make_case(db_session, "ISO-A", a.id)

    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"email": "a@example.com", "password": "password123"})
    resp = client.get("/cases/ISO-A")
    assert resp.status_code == 200
