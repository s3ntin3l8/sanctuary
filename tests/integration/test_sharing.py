"""Phase 3 — case sharing: viewer/editor access, sharing routes, reassign-on-delete."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, CaseShare
from app.models.enums import CaseAccessLevel, CaseStatus, Jurisdiction, UserRole
from app.services import access_service, auth_service


def _client():
    return TestClient(app, follow_redirects=False)


def _login(client, email, password="password123"):
    client.post("/login", data={"email": email, "password": password})


@pytest.fixture
def owner_and_other(db_session):
    owner = auth_service.create_user(
        db_session, email="owner@example.com", password="password123"
    )
    other = auth_service.create_user(
        db_session, email="other@example.com", password="password123"
    )
    db_session.commit()
    case = Case(
        id="SH-1",
        title="Shared Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=owner.id,
    )
    db_session.add(case)
    db_session.commit()
    return owner, other, case


def _share(db, case_id, user_id, level, by):
    db.add(CaseShare(case_id=case_id, user_id=user_id, permission=level, granted_by=by))
    db.commit()


# --- service layer ---------------------------------------------------------


def test_viewer_can_view_not_edit(db_session, owner_and_other):
    owner, other, case = owner_and_other
    _share(db_session, case.id, other.id, CaseAccessLevel.VIEWER, owner.id)
    assert access_service.can_view_case(db_session, other, case) is True
    assert access_service.can_edit_case(db_session, other, case) is False


def test_editor_can_view_and_edit(db_session, owner_and_other):
    owner, other, case = owner_and_other
    _share(db_session, case.id, other.id, CaseAccessLevel.EDITOR, owner.id)
    assert access_service.can_view_case(db_session, other, case) is True
    assert access_service.can_edit_case(db_session, other, case) is True


def test_visible_case_ids_includes_shared(db_session, owner_and_other):
    owner, other, case = owner_and_other
    _share(db_session, case.id, other.id, CaseAccessLevel.VIEWER, owner.id)
    assert case.id in access_service.visible_case_ids(db_session, other)


def test_unshared_user_cannot_view(db_session, owner_and_other):
    owner, other, case = owner_and_other
    assert access_service.can_view_case(db_session, other, case) is False


# --- routes ----------------------------------------------------------------


def test_owner_can_share_and_grantee_sees_case(
    auth_enabled, db_session, owner_and_other
):
    owner, other, case = owner_and_other
    client = _client()
    _login(client, "owner@example.com")
    resp = client.post(
        f"/cases/{case.id}/shares",
        data={"email": "other@example.com", "permission": "viewer"},
    )
    assert resp.status_code == 204

    grantee = _client()
    _login(grantee, "other@example.com")
    assert grantee.get(f"/cases/{case.id}").status_code == 200


def test_non_owner_cannot_open_sharing_page(auth_enabled, db_session, owner_and_other):
    owner, other, case = owner_and_other
    client = _client()
    _login(client, "other@example.com")
    assert client.get(f"/cases/{case.id}/sharing").status_code == 404


def test_remove_share_revokes_access(auth_enabled, db_session, owner_and_other):
    owner, other, case = owner_and_other
    _share(db_session, case.id, other.id, CaseAccessLevel.VIEWER, owner.id)

    client = _client()
    _login(client, "owner@example.com")
    resp = client.post(f"/cases/{case.id}/shares/{other.id}/remove")
    assert resp.status_code == 204

    grantee = _client()
    _login(grantee, "other@example.com")
    assert grantee.get(f"/cases/{case.id}").status_code == 404


def test_reassign_then_delete_user(auth_enabled, db_session, owner_and_other):
    owner, other, case = owner_and_other
    auth_service.create_user(
        db_session, email="adm@example.com", password="password123", role=UserRole.ADMIN
    )
    db_session.commit()

    client = _client()
    _login(client, "adm@example.com")
    # owner owns SH-1 → delete blocked
    assert client.post(f"/admin/users/{owner.id}/delete").status_code == 409
    # reassign to other, then delete succeeds
    assert (
        client.post(
            f"/admin/users/{owner.id}/reassign-cases", data={"new_owner_id": other.id}
        ).status_code
        == 204
    )
    db_session.expire_all()
    assert db_session.get(Case, "SH-1").owner_id == other.id
    assert client.post(f"/admin/users/{owner.id}/delete").status_code == 204
