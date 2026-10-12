"""Agent API tokens: lifecycle, the /mcp bearer gate, and the settings routes."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.timezone import now_utc
from app.main import app
from app.models.database import ApiToken, ApiTokenCase, AuditLog, Case
from app.models.enums import (
    ApiTokenAccess,
    AuditEventType,
    CaseStatus,
    Jurisdiction,
)
from app.services import api_token_service, auth_service, merge_service

PASSWORD = "password123"  # pragma: allowlist secret


def _case(db, case_id, owner_id):
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


def _login(email: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    return client


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def alice(db_session):
    user = auth_service.create_user(
        db_session, email="alice@example.com", password=PASSWORD
    )
    db_session.commit()
    return user


def _mint(db, user, **kw):
    kw.setdefault("access", ApiTokenAccess.READ)
    kw.setdefault("all_cases", True)
    token, secret = api_token_service.mint(db, user, name="agent", **kw)
    db.commit()
    return token, secret


# --- service -----------------------------------------------------------------


def test_only_the_hash_is_stored(db_session, alice):
    token, secret = _mint(db_session, alice)
    assert secret.startswith("sct_")
    assert token.token_hash != secret
    assert secret not in (token.token_hash, token.prefix)
    assert secret.startswith(token.prefix)


def test_verify_accepts_live_token(db_session, alice):
    _, secret = _mint(db_session, alice)
    assert api_token_service.verify(db_session, secret).user_id == alice.id


@pytest.mark.parametrize("secret", ["", "nope", "sct_unknown"])
def test_verify_rejects_unknown_secrets(db_session, alice, secret):
    assert api_token_service.verify(db_session, secret) is None


def test_verify_rejects_revoked_expired_and_disabled_owner(db_session, alice):
    token, secret = _mint(db_session, alice)
    api_token_service.revoke(db_session, token, actor_user_id=alice.id)
    db_session.commit()
    assert api_token_service.verify(db_session, secret) is None

    token, secret = _mint(db_session, alice)
    token.expires_at = now_utc() - timedelta(seconds=1)
    db_session.commit()
    assert api_token_service.verify(db_session, secret) is None

    _, secret = _mint(db_session, alice)
    alice.is_active = False
    db_session.commit()
    assert api_token_service.verify(db_session, secret) is None


def test_per_case_scope_requires_visible_cases(db_session, alice):
    other = auth_service.create_user(
        db_session, email="bob@example.com", password=PASSWORD
    )
    _case(db_session, "A-1", alice.id)
    _case(db_session, "B-1", other.id)
    with pytest.raises(api_token_service.TokenScopeError):
        _mint(db_session, alice, all_cases=False, case_ids=[])
    with pytest.raises(api_token_service.TokenScopeError):
        _mint(db_session, alice, all_cases=False, case_ids=["B-1"])
    token, _ = _mint(db_session, alice, all_cases=False, case_ids=["A-1"])
    assert [c.case_id for c in token.cases] == ["A-1"]


def test_all_cases_token_stores_no_case_rows(db_session, alice):
    token, _ = _mint(db_session, alice, all_cases=True)
    assert token.all_cases is True
    assert token.cases == []


def test_all_cases_token_rejects_a_case_list(db_session, alice):
    _case(db_session, "A-1", alice.id)
    with pytest.raises(api_token_service.TokenScopeError):
        _mint(db_session, alice, all_cases=True, case_ids=["A-1"])


def test_verify_is_a_pure_read_and_touch_throttles(db_session, alice):
    token, secret = _mint(db_session, alice)
    assert api_token_service.verify(db_session, secret).last_used_at is None
    api_token_service.touch(db_session, token)
    first = token.last_used_at
    assert first is not None
    api_token_service.touch(db_session, token)
    assert token.last_used_at == first


def test_mint_and_revoke_are_audited(db_session, alice):
    token, _ = _mint(db_session, alice)
    api_token_service.revoke(db_session, token, actor_user_id=alice.id)
    db_session.commit()
    events = {r.event_type for r in db_session.query(AuditLog)}
    assert {
        AuditEventType.API_TOKEN_CREATED,
        AuditEventType.API_TOKEN_REVOKED,
    } <= events


def test_merging_cases_moves_token_scope(db_session, alice):
    _case(db_session, "SRC-1", alice.id)
    _case(db_session, "DST-1", alice.id)
    only_src, _ = _mint(db_session, alice, all_cases=False, case_ids=["SRC-1"])
    both, _ = _mint(db_session, alice, all_cases=False, case_ids=["SRC-1", "DST-1"])
    merge_service.merge_cases(db_session, "SRC-1", "DST-1", actor_user_id=alice.id)
    db_session.commit()
    scope = lambda t: sorted(  # noqa: E731
        r.case_id for r in db_session.query(ApiTokenCase).filter_by(token_id=t.id)
    )
    assert scope(only_src) == ["DST-1"]
    assert scope(both) == ["DST-1"]


def test_merging_twice_keeps_token_scope_on_the_final_case(db_session, alice):
    for cid in ("SRC-1", "MID-1", "DST-1"):
        _case(db_session, cid, alice.id)
    token, _ = _mint(db_session, alice, all_cases=False, case_ids=["SRC-1"])
    merge_service.merge_cases(db_session, "SRC-1", "MID-1", actor_user_id=alice.id)
    merge_service.merge_cases(db_session, "MID-1", "DST-1", actor_user_id=alice.id)
    db_session.commit()
    rows = db_session.query(ApiTokenCase).filter_by(token_id=token.id).all()
    assert [r.case_id for r in rows] == ["DST-1"]


# --- /mcp bearer gate --------------------------------------------------------


def test_mcp_requires_a_token(auth_enabled, db_session):
    resp = TestClient(app).post("/mcp", json={})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_mcp_requires_a_token_even_when_auth_is_disabled(db_session):
    # conftest defaults AUTH_ENABLED=false (dev mode) — /mcp must still gate.
    assert TestClient(app).post("/mcp", json={}).status_code == 401


def test_mcp_ignores_the_session_cookie(auth_enabled, db_session, alice):
    client = _login("alice@example.com")
    assert client.post("/mcp", json={}).status_code == 401


def test_valid_token_passes_the_gate(auth_enabled, db_session, alice):
    _, secret = _mint(db_session, alice)
    resp = TestClient(app).post("/mcp", json={}, headers=_bearer(secret))
    assert resp.status_code != 401


@pytest.mark.parametrize("header", ["Bearer   {s}", "bearer {s}", "BEARER {s}"])
def test_bearer_scheme_is_case_and_space_tolerant(
    auth_enabled, db_session, alice, header
):
    _, secret = _mint(db_session, alice)
    resp = TestClient(app).post(
        "/mcp", json={}, headers={"Authorization": header.format(s=secret)}
    )
    assert resp.status_code != 401


def test_bearer_without_a_space_is_rejected(auth_enabled, db_session, alice):
    _, secret = _mint(db_session, alice)
    resp = TestClient(app).post(
        "/mcp", json={}, headers={"Authorization": f"Bearer{secret}"}
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("stamp_first", [False, True])
def test_a_failing_last_used_write_does_not_fail_the_call(
    auth_enabled, db_session, alice, monkeypatch, stamp_first
):
    def boom(db, token):
        if stamp_first:  # the commit fails after the timestamp was set
            token.last_used_at = now_utc()
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(api_token_service, "touch", boom)
    token, secret = _mint(db_session, alice)
    resp = TestClient(app).post("/mcp", json={}, headers=_bearer(secret))
    assert resp.status_code not in (401, 500)
    db_session.expire_all()
    assert db_session.get(ApiToken, token.id).last_used_at is None


def test_revoked_token_is_rejected(auth_enabled, db_session, alice):
    token, secret = _mint(db_session, alice)
    api_token_service.revoke(db_session, token, actor_user_id=alice.id)
    db_session.commit()
    resp = TestClient(app).post("/mcp", json={}, headers=_bearer(secret))
    assert resp.status_code == 401


def test_bearer_token_does_not_open_the_rest_api(auth_enabled, db_session, alice):
    _, secret = _mint(db_session, alice)
    resp = TestClient(app).get("/api/v1/cases", headers=_bearer(secret))
    assert resp.status_code == 401


def test_cross_origin_bearer_post_is_not_blocked(auth_enabled, db_session, alice):
    _, secret = _mint(db_session, alice)
    headers = {**_bearer(secret), "Origin": "https://inspector.example"}
    resp = TestClient(app).post("/mcp", json={}, headers=headers)
    # Past the gate and the origin guard; whatever the route then answers.
    assert resp.status_code not in (401, 403)


def test_token_is_rate_limited(auth_enabled, db_session, alice, monkeypatch):
    from limits import parse

    from app.core import rate_limit

    monkeypatch.setattr(rate_limit, "MCP_TOKEN_RATE_LIMIT", parse("2/minute"))
    _, secret = _mint(db_session, alice)
    client = TestClient(app)
    codes = [
        client.post("/mcp", json={}, headers=_bearer(secret)).status_code
        for _ in range(3)
    ]
    assert codes[-1] == 429


# --- settings routes ---------------------------------------------------------


def test_settings_create_list_revoke(auth_enabled, db_session, alice):
    _case(db_session, "A-1", alice.id)
    client = _login("alice@example.com")

    created = client.post(
        "/api/v1/settings/agent-access",
        json={
            "name": "Claude Code",
            "access": "read_write",
            "all_cases": False,
            "case_ids": ["A-1"],
            "expires_in_days": 30,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["secret"].startswith("sct_")
    assert body["token"]["cases"] == [{"id": "A-1", "title": "Case A-1"}]
    assert body["token"]["expires_at"] is not None

    listing = client.get("/api/v1/settings/agent-access").json()
    assert listing["mcp_url"].endswith("/mcp")
    assert [t["name"] for t in listing["tokens"]] == ["Claude Code"]
    assert "secret" not in listing["tokens"][0]

    revoked = client.delete(f"/api/v1/settings/agent-access/{body['token']['id']}")
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    resp = TestClient(app).post("/mcp", json={}, headers=_bearer(body["secret"]))
    assert resp.status_code == 401


def test_settings_rejects_empty_or_foreign_scope(auth_enabled, db_session, alice):
    other = auth_service.create_user(
        db_session, email="bob@example.com", password=PASSWORD
    )
    _case(db_session, "B-1", other.id)
    client = _login("alice@example.com")
    for all_cases, case_ids in ((False, []), (False, ["B-1"]), (True, ["B-1"])):
        resp = client.post(
            "/api/v1/settings/agent-access",
            json={
                "name": "x",
                "access": "read",
                "all_cases": all_cases,
                "case_ids": case_ids,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_scope"


def test_settings_rejects_a_name_without_letters_or_digits(
    auth_enabled, db_session, alice
):
    client = _login("alice@example.com")
    resp = client.post(
        "/api/v1/settings/agent-access",
        json={"name": " . ", "access": "read", "all_cases": True, "case_ids": []},
    )
    assert resp.status_code == 422


def test_users_cannot_see_or_revoke_each_others_tokens(auth_enabled, db_session, alice):
    bob = auth_service.create_user(
        db_session, email="bob@example.com", password=PASSWORD
    )
    token, _ = _mint(db_session, bob)
    client = _login("alice@example.com")
    assert client.get("/api/v1/settings/agent-access").json()["tokens"] == []
    assert client.delete(f"/api/v1/settings/agent-access/{token.id}").status_code == 404
    assert db_session.get(ApiToken, token.id).revoked_at is None


def test_agent_access_page_is_served(auth_enabled, db_session, alice):
    client = _login("alice@example.com")
    assert client.get("/settings/agent-access").status_code == 200
