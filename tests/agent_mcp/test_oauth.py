"""OAuth 2.1 for MCP clients: discovery, registration, consent, tokens, revocation."""

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from limits import parse

from app.core import rate_limit
from app.core.timezone import now_utc
from app.main import app
from app.models.database import ApiToken, OAuthCode, OAuthGrant
from app.models.enums import ApiTokenKind
from app.services import api_token_service

from .conftest import PASSWORD, McpSession

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(client: TestClient, **overrides):
    body = {
        "client_name": "Claude",
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        **overrides,
    }
    return client.post("/register", json=body)


def login(client: TestClient, email: str = "alice@example.com") -> None:
    resp = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert resp.status_code == 200


def authorize_params(client_id: str, challenge: str, **extra) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        **extra,
    }


def consent(client: TestClient, params: dict[str, str], **decision):
    """The consent page's POST for the redirect the SDK sent the browser to."""
    return client.post(
        "/api/v1/oauth/authorize",
        json={
            "client_id": params["client_id"],
            "redirect_uri": params["redirect_uri"],
            "code_challenge": params["code_challenge"],
            "state": params.get("state"),
            "decision": "approve",
            "access": "read",
            "all_cases": True,
            "case_ids": [],
            **decision,
        },
    )


def code_from(result) -> str:
    assert result.status_code == 200, result.text
    return parse_qs(urlparse(result.json()["redirect_url"]).query)["code"][0]


def token_request(client: TestClient, client_id: str, **form):
    return client.post("/token", data={"client_id": client_id, **form})


def exchange(client: TestClient, client_id: str, code: str, verifier: str):
    return token_request(
        client,
        client_id,
        grant_type="authorization_code",
        code=code,
        redirect_uri=CALLBACK,
        code_verifier=verifier,
    )


@pytest.fixture
def flow(auth_enabled, mcp_client, alice, make_case):
    """A registered client plus a signed-in user, ready to authorize."""
    make_case("A-1", alice.id)
    make_case("A-2", alice.id)
    client_id = register(mcp_client).json()["client_id"]
    login(mcp_client)
    return mcp_client, client_id


def connect(client, client_id, **decision) -> dict:
    """Run authorize → consent → token; returns the token response."""
    verifier, challenge = pkce()
    params = authorize_params(client_id, challenge)
    code = code_from(consent(client, params, **decision))
    resp = exchange(client, client_id, code, verifier)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- discovery ------------------------------------------------------------------


def test_metadata_is_served_without_a_session(auth_enabled, db_session):
    client = TestClient(app)
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    assert meta["token_endpoint"].endswith("/token")
    assert meta["registration_endpoint"].endswith("/register")
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        resource = client.get(path).json()
        assert resource["resource"].endswith("/mcp")
        assert resource["authorization_servers"] == [meta["issuer"]]


def test_unauthorised_mcp_call_points_clients_at_discovery(mcp_client, db_session):
    resp = mcp_client.post("/mcp", json={})
    assert resp.status_code == 401
    header = resp.headers["www-authenticate"]
    assert header.startswith("Bearer resource_metadata=")
    assert "/.well-known/oauth-protected-resource/mcp" in header


# --- registration ---------------------------------------------------------------


def test_registers_a_public_client(auth_enabled, mcp_client, db_session):
    resp = register(mcp_client)
    assert resp.status_code == 201
    assert resp.json()["client_id"] and not resp.json().get("client_secret")


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_endpoint_auth_method": "client_secret_post"},
        {"redirect_uris": ["javascript:alert(1)"]},
        {"redirect_uris": ["http://evil.example/cb"]},
        {"redirect_uris": ["https://claude.ai/cb#frag"]},
    ],
)
def test_registration_rejects_unsafe_clients(
    auth_enabled, mcp_client, db_session, overrides
):
    assert register(mcp_client, **overrides).status_code == 400


def test_loopback_and_app_scheme_redirects_are_allowed(
    auth_enabled, mcp_client, db_session
):
    for uri in ("http://localhost:6274/oauth/callback", "cursor://anysphere/cb"):
        assert register(mcp_client, redirect_uris=[uri]).status_code == 201


def test_registration_is_rate_limited_per_ip(auth_enabled, db_session):
    for _ in range(2):
        assert rate_limit.hit_ip_limit("probe", parse("2/hour"), "1.2.3.4")
    assert not rate_limit.hit_ip_limit("probe", parse("2/hour"), "1.2.3.4")
    assert rate_limit.hit_ip_limit("probe", parse("2/hour"), "5.6.7.8")


# --- authorize ------------------------------------------------------------------


def test_authorize_sends_the_browser_to_the_consent_page(flow):
    client, client_id = flow
    _, challenge = pkce()
    resp = client.get(
        "/authorize",
        params=authorize_params(client_id, challenge),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = urlparse(resp.headers["location"])
    assert location.path == "/oauth/authorize"
    query = parse_qs(location.query)
    assert query["client_id"] == [client_id] and query["state"] == ["xyz"]


def test_authorize_is_reachable_without_a_session(auth_enabled, mcp_client, db_session):
    client_id = register(mcp_client).json()["client_id"]
    _, challenge = pkce()
    resp = TestClient(app).get(
        "/authorize",
        params=authorize_params(client_id, challenge),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/oauth/authorize?")


def test_authorize_rejects_plain_pkce_and_unregistered_redirects(flow):
    client, client_id = flow
    _, challenge = pkce()
    plain = client.get(
        "/authorize",
        params=authorize_params(client_id, challenge, code_challenge_method="plain"),
        follow_redirects=False,
    )
    assert "/oauth/authorize" not in plain.headers.get("location", "")
    assert "error" in (plain.headers.get("location") or plain.text)
    foreign = client.get(
        "/authorize",
        params=authorize_params(
            client_id, challenge, redirect_uri="https://evil.example/cb"
        ),
        follow_redirects=False,
    )
    assert foreign.status_code == 400  # shown to the user, never redirected to evil


def test_consent_page_needs_a_login(auth_enabled, db_session):
    resp = TestClient(app, follow_redirects=False).get(
        "/oauth/authorize?client_id=x&state=s"
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=%2Foauth%2Fauthorize")


# --- consent API ----------------------------------------------------------------


def test_authorization_request_names_the_client_and_redirect_host(flow):
    client, client_id = flow
    _, challenge = pkce()
    resp = client.get(
        "/api/v1/oauth/authorization-request",
        params={
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "client_name": "Claude",
        "redirect_host": "claude.ai",
        "mcp_enabled": True,
    }


@pytest.mark.parametrize(
    "override",
    [
        {"redirect_uri": "https://evil.example/cb"},
        {"client_id": "nope"},
        {"code_challenge": "short"},
    ],
)
def test_consent_validates_what_the_page_echoes(flow, override):
    client, client_id = flow
    _, challenge = pkce()
    params = {
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        **override,
    }
    assert (
        client.get("/api/v1/oauth/authorization-request", params=params).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/oauth/authorize", json={**params, "decision": "approve"}
        ).status_code
        == 422
    )


def test_denying_redirects_back_with_access_denied_and_state(flow):
    client, client_id = flow
    _, challenge = pkce()
    params = authorize_params(client_id, challenge)
    result = consent(client, params, decision="deny")
    query = parse_qs(urlparse(result.json()["redirect_url"]).query)
    assert query == {"error": ["access_denied"], "state": ["xyz"]}


def test_consent_post_is_origin_guarded(flow):
    client, client_id = flow
    _, challenge = pkce()
    resp = client.post(
        "/api/v1/oauth/authorize",
        headers={"Origin": "https://evil.example"},
        json={
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "decision": "approve",
            "all_cases": True,
        },
    )
    assert resp.status_code == 403


def test_consent_rejects_a_case_the_user_cannot_see(flow, bob, make_case):
    client, client_id = flow
    make_case("B-1", bob.id)
    _, challenge = pkce()
    result = consent(
        client,
        authorize_params(client_id, challenge),
        all_cases=False,
        case_ids=["B-1"],
    )
    assert result.status_code == 422 and result.json()["code"] == "invalid_scope"


# --- tokens ---------------------------------------------------------------------


def test_full_flow_gives_a_working_scoped_token(flow, db_session):
    client, client_id = flow
    tokens = connect(
        client, client_id, all_cases=False, case_ids=["A-1"], access="read"
    )
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
    assert tokens["refresh_token"].startswith("scr_")

    session = McpSession(TestClient(app), "unused")  # replaced below
    session = McpSession(client, tokens["access_token"])
    ids = [c["case_id"] for c in session.call("list_cases")[1]["result"]]
    assert ids == ["A-1"]  # only the case the user picked on the consent page
    error, message = session.call(
        "create_action_item",
        case_id="A-1",
        title="x",
        due_date="2030-01-01",
        action_type="deadline",
    )
    assert error and "read-only" in message

    token = db_session.query(ApiToken).filter_by(kind=ApiTokenKind.OAUTH).one()
    assert token.name == "Claude" and token.oauth_client_id == client_id
    assert token.expires_at is None  # lifetime is the grant's, not the row's


def test_all_cases_grant_sees_every_case(flow):
    client, client_id = flow
    session = McpSession(client, connect(client, client_id)["access_token"])
    assert len(session.call("list_cases")[1]["result"]) == 2


def test_token_endpoint_works_from_a_browser_origin(flow):
    client, client_id = flow
    resp = client.post(
        "/token",
        headers={"Origin": "https://inspector.example"},
        data={"client_id": client_id, "grant_type": "authorization_code"},
    )
    assert resp.status_code == 400  # a malformed grant, not the origin guard's 403


def test_wrong_code_verifier_is_rejected(flow):
    client, client_id = flow
    _, challenge = pkce()
    code = code_from(consent(client, authorize_params(client_id, challenge)))
    resp = exchange(client, client_id, code, pkce()[0])
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


def test_a_code_works_once(flow):
    client, client_id = flow
    verifier, challenge = pkce()
    code = code_from(consent(client, authorize_params(client_id, challenge)))
    assert exchange(client, client_id, code, verifier).status_code == 200
    again = exchange(client, client_id, code, verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_an_expired_code_is_rejected(flow, db_session):
    client, client_id = flow
    verifier, challenge = pkce()
    code = code_from(consent(client, authorize_params(client_id, challenge)))
    db_session.query(OAuthCode).update({"expires_at": now_utc() - timedelta(minutes=1)})
    db_session.commit()
    assert exchange(client, client_id, code, verifier).status_code == 400


def test_a_code_is_bound_to_its_redirect_uri(flow):
    client, client_id = flow
    verifier, challenge = pkce()
    code = code_from(consent(client, authorize_params(client_id, challenge)))
    resp = token_request(
        client,
        client_id,
        grant_type="authorization_code",
        code=code,
        redirect_uri="https://evil.example/cb",
        code_verifier=verifier,
    )
    assert resp.status_code == 400


def test_only_hashes_are_stored(flow, db_session):
    client, client_id = flow
    tokens = connect(client, client_id)
    grant = db_session.query(OAuthGrant).one()
    token = db_session.query(ApiToken).one()
    assert grant.refresh_hash != tokens["refresh_token"]
    assert token.token_hash != tokens["access_token"]
    assert token.token_hash == api_token_service.hash_secret(tokens["access_token"])


# --- refresh, expiry, revocation ------------------------------------------------


def refresh(client, client_id, refresh_token):
    return token_request(
        client, client_id, grant_type="refresh_token", refresh_token=refresh_token
    )


def mcp_status(client, access_token: str) -> int:
    return client.post(
        "/mcp",
        json={},
        headers={"Authorization": f"Bearer {access_token}"},
    ).status_code


def test_refresh_rotates_both_secrets(flow):
    client, client_id = flow
    first = connect(client, client_id)
    second = refresh(client, client_id, first["refresh_token"]).json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert mcp_status(client, first["access_token"]) == 401  # old access token dead
    assert mcp_status(client, second["access_token"]) != 401


def test_reusing_a_rotated_refresh_token_revokes_the_grant(flow, db_session):
    client, client_id = flow
    first = connect(client, client_id)
    second = refresh(client, client_id, first["refresh_token"]).json()
    stale = refresh(client, client_id, first["refresh_token"])
    assert stale.status_code == 400 and stale.json()["error"] == "invalid_grant"
    # the legitimate holder is cut off too: the leak invalidates the whole grant
    assert refresh(client, client_id, second["refresh_token"]).status_code == 400
    assert mcp_status(client, second["access_token"]) == 401
    assert db_session.query(ApiToken).one().revoked_at is not None


def test_an_expired_access_token_is_rejected_but_refreshable(flow, db_session):
    client, client_id = flow
    tokens = connect(client, client_id)
    db_session.query(OAuthGrant).update(
        {"access_expires_at": now_utc() - timedelta(seconds=1)}
    )
    db_session.commit()
    assert mcp_status(client, tokens["access_token"]) == 401
    renewed = refresh(client, client_id, tokens["refresh_token"])
    assert renewed.status_code == 200
    assert mcp_status(client, renewed.json()["access_token"]) != 401


def test_a_lapsed_refresh_token_is_rejected(flow, db_session):
    client, client_id = flow
    tokens = connect(client, client_id)
    db_session.query(OAuthGrant).update(
        {"refresh_expires_at": now_utc() - timedelta(seconds=1)}
    )
    db_session.commit()
    assert refresh(client, client_id, tokens["refresh_token"]).status_code == 400


def test_refresh_is_bound_to_the_client(flow):
    client, client_id = flow
    tokens = connect(client, client_id)
    other = register(client, client_name="Other").json()["client_id"]
    assert refresh(client, other, tokens["refresh_token"]).status_code == 400


def test_revocation_by_another_client_does_nothing(flow, db_session):
    client, client_id = flow
    tokens = connect(client, client_id)
    other = register(client, client_name="Other").json()["client_id"]
    resp = client.post(
        "/revoke", data={"client_id": other, "token": tokens["access_token"]}
    )
    assert resp.status_code == 200
    assert mcp_status(client, tokens["access_token"]) != 401


def test_the_revocation_endpoint_kills_the_grant(flow, db_session):
    client, client_id = flow
    tokens = connect(client, client_id)
    resp = client.post(
        "/revoke", data={"client_id": client_id, "token": tokens["access_token"]}
    )
    assert resp.status_code == 200
    assert mcp_status(client, tokens["access_token"]) == 401
    assert refresh(client, client_id, tokens["refresh_token"]).status_code == 400
    assert db_session.query(ApiToken).one().revoked_at is not None


def test_revoking_in_settings_kills_the_oauth_grant(flow, db_session):
    client, client_id = flow
    tokens = connect(client, client_id)
    listing = client.get("/api/v1/settings/agent-access").json()["tokens"]
    assert listing[0]["kind"] == "oauth" and listing[0]["oauth_client_name"] == "Claude"
    assert listing[0]["expires_at"] is not None  # the refresh horizon
    client.delete(f"/api/v1/settings/agent-access/{listing[0]['id']}")
    assert mcp_status(client, tokens["access_token"]) == 401
    assert refresh(client, client_id, tokens["refresh_token"]).status_code == 400


def test_static_tokens_still_work(flow, alice, make_token):
    client, _ = flow
    secret = make_token(alice)
    assert mcp_status(client, secret) != 401


def test_an_oauth_token_is_not_a_cookie_session(flow):
    client, client_id = flow
    tokens = connect(client, client_id)
    plain = TestClient(app)
    resp = plain.get(
        "/api/v1/cases", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert resp.status_code == 401


# --- instance switch ------------------------------------------------------------


def test_switching_mcp_off_stops_oauth_too(flow, db_session, alice):
    client, client_id = flow
    tokens = connect(client, client_id)
    api_token_service.set_mcp_enabled(db_session, False, actor_user_id=alice.id)
    db_session.commit()
    from app.core.cache import cache

    cache.clear()
    _, challenge = pkce()
    assert mcp_status(client, tokens["access_token"]) == 503
    assert client.post("/token", data={"client_id": client_id}).status_code == 503
    assert register(client).status_code == 503
    assert (
        client.get(
            "/authorize",
            params=authorize_params(client_id, challenge),
            follow_redirects=False,
        ).status_code
        == 503
    )
    result = consent(client, authorize_params(client_id, challenge))
    assert result.status_code == 409 and result.json()["code"] == "mcp_disabled"
    request = client.get(
        "/api/v1/oauth/authorization-request",
        params={
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
        },
    )
    assert request.json()["mcp_enabled"] is False
