"""Admin → Agent access: all users' tokens, admin revoke, activity, instance switch."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import ApiToken, AuditLog
from app.models.enums import ApiTokenAccess, AuditEventType, UserRole
from app.services import api_token_service, audit_service, auth_service

PASSWORD = "password123"  # pragma: allowlist secret
BASE = "/api/v1/admin/agent-access"


def _login(email: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    return client


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def admin(db_session):
    user = auth_service.create_user(
        db_session, email="root@example.com", password=PASSWORD, role=UserRole.ADMIN
    )
    db_session.commit()
    return user


@pytest.fixture
def alice(db_session):
    user = auth_service.create_user(
        db_session, email="alice@example.com", password=PASSWORD, display_name="Alice"
    )
    db_session.commit()
    return user


def _mint(db, user, name="agent"):
    token, secret = api_token_service.mint(
        db, user, name=name, access=ApiTokenAccess.READ, all_cases=True
    )
    db.commit()
    return token, secret


def _agent_action(db, token: ApiToken, tool: str):
    audit_service.record(
        db,
        AuditEventType.MCP_WRITE,
        target_type="document",
        target_id="1",
        payload={"tool": tool, "api_token_id": token.id, "note": "x"},
        actor_user_id=token.user_id,
        actor_label=f"mcp:{token.name}",
    )
    db.commit()


# --- access ------------------------------------------------------------------


def test_every_admin_route_rejects_non_admins(auth_enabled, db_session, admin, alice):
    token, _ = _mint(db_session, alice)
    client = _login("alice@example.com")
    assert client.get(BASE).status_code == 403
    assert client.put(f"{BASE}/enabled", json={"enabled": False}).status_code == 403
    assert client.delete(f"{BASE}/tokens/{token.id}").status_code == 403
    assert client.get(f"{BASE}/tokens/{token.id}/activity").status_code == 403
    assert db_session.get(ApiToken, token.id).revoked_at is None


def test_admin_page_is_admin_only(auth_enabled, db_session, admin, alice):
    assert _login("root@example.com").get("/admin/agent-access").status_code == 200
    assert _login("alice@example.com").get("/admin/agent-access").status_code == 403


# --- overview, revoke, activity ----------------------------------------------


def test_admin_sees_every_users_tokens(auth_enabled, db_session, admin, alice):
    _mint(db_session, alice, "alice-agent")
    _mint(db_session, admin, "admin-agent")
    body = _login("root@example.com").get(BASE).json()
    assert body["mcp_enabled"] is True
    by_name = {t["name"]: t for t in body["tokens"]}
    assert set(by_name) == {"alice-agent", "admin-agent"}
    assert by_name["alice-agent"]["owner_email"] == "alice@example.com"
    assert by_name["alice-agent"]["owner_name"] == "Alice"
    assert by_name["alice-agent"]["status"] == "active"
    assert "secret" not in by_name["alice-agent"]


def test_admin_revoke_kills_the_token(auth_enabled, db_session, admin, alice):
    token, secret = _mint(db_session, alice)
    resp = _login("root@example.com").delete(f"{BASE}/tokens/{token.id}")
    assert resp.status_code == 200
    assert resp.json()["tokens"][0]["status"] == "revoked"
    assert (
        TestClient(app).post("/mcp", json={}, headers=_bearer(secret)).status_code
        == 401
    )
    event = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.API_TOKEN_REVOKED)
        .one()
    )
    assert event.actor_user_id == admin.id
    assert event.payload["by_admin"] is True
    assert event.payload["api_token_id"] == token.id
    assert event.payload["owner_user_id"] == alice.id


def test_revoking_an_unknown_token_is_404(auth_enabled, db_session, admin):
    assert _login("root@example.com").delete(f"{BASE}/tokens/999").status_code == 404


def test_activity_lists_only_that_tokens_agent_actions(
    auth_enabled, db_session, admin, alice
):
    mine, _ = _mint(db_session, alice, "mine")
    other, _ = _mint(db_session, alice, "other")
    _agent_action(db_session, mine, "add_pin")
    _agent_action(db_session, mine, "create_claim")
    _agent_action(db_session, other, "set_reaction")

    client = _login("root@example.com")
    entries = client.get(f"{BASE}/tokens/{mine.id}/activity").json()["entries"]
    assert [e["tool"] for e in entries] == ["create_claim", "add_pin"]
    assert all(e["event_type"] == "mcp_write" for e in entries)
    assert entries[0]["detail"] == {"note": "x"}  # envelope keys are not repeated

    counts = {
        t["name"]: t["activity_count_7d"] for t in client.get(BASE).json()["tokens"]
    }
    assert counts == {"mine": 2, "other": 1}
    assert (
        client.get(f"{BASE}/tokens/{mine.id}/activity?limit=1").json()["entries"][0][
            "tool"
        ]
        == "create_claim"
    )


def test_mint_and_revoke_audit_rows_name_the_token(db_session, alice):
    token, _ = _mint(db_session, alice)
    api_token_service.revoke(db_session, token, actor_user_id=alice.id)
    db_session.commit()
    rows = db_session.query(AuditLog).all()
    assert {r.payload["api_token_id"] for r in rows} == {token.id}


# --- instance switch ---------------------------------------------------------


def test_switch_off_blocks_mcp_even_with_a_valid_token(
    auth_enabled, db_session, admin, alice
):
    _, secret = _mint(db_session, alice)
    admin_client = _login("root@example.com")
    assert (
        admin_client.put(f"{BASE}/enabled", json={"enabled": False}).status_code == 200
    )

    resp = TestClient(app).post("/mcp", json={}, headers=_bearer(secret))
    assert resp.status_code == 503
    assert resp.json()["code"] == "mcp_disabled"
    # Refused before any token check, so even garbage gets the same answer.
    assert TestClient(app).post("/mcp", json={}).json()["code"] == "mcp_disabled"


def test_switch_off_blocks_minting_and_flags_the_user_page(
    auth_enabled, db_session, admin, alice
):
    _login("root@example.com").put(f"{BASE}/enabled", json={"enabled": False})
    client = _login("alice@example.com")
    assert client.get("/api/v1/settings/agent-access").json()["mcp_enabled"] is False
    resp = client.post(
        "/api/v1/settings/agent-access",
        json={"name": "x", "access": "read", "all_cases": True, "case_ids": []},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "mcp_disabled"


def test_switch_back_on_restores_access(auth_enabled, db_session, admin, alice):
    _, secret = _mint(db_session, alice)
    admin_client = _login("root@example.com")
    admin_client.put(f"{BASE}/enabled", json={"enabled": False})
    admin_client.put(f"{BASE}/enabled", json={"enabled": True})

    resp = TestClient(app).post("/mcp", json={}, headers=_bearer(secret))
    assert resp.status_code != 401
    assert resp.json().get("code") != "mcp_disabled"
    assert (
        _login("alice@example.com")
        .get("/api/v1/settings/agent-access")
        .json()["mcp_enabled"]
        is True
    )


def test_toggling_is_audited(auth_enabled, db_session, admin):
    client = _login("root@example.com")
    client.put(f"{BASE}/enabled", json={"enabled": False})
    client.put(f"{BASE}/enabled", json={"enabled": True})
    events = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.MCP_SWITCH_CHANGED)
        .order_by(AuditLog.id)
        .all()
    )
    assert [(e.payload["enabled"], e.payload["previous"]) for e in events] == [
        (False, True),
        (True, False),
    ]
    assert all(e.actor_user_id == admin.id for e in events)


def test_switch_defaults_to_on(db_session):
    assert api_token_service.mcp_enabled(db_session) is True
