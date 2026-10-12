"""The MCP endpoint end to end: protocol, scope and the case tools."""

from app.models.enums import ApiTokenAccess


def _result(payload):
    # Tools returning a list are wrapped as {"result": [...]} in structuredContent.
    return (
        payload["result"]
        if isinstance(payload, dict) and "result" in payload
        else payload
    )


def test_server_lists_tools(mcp_session, make_token, alice):
    session = mcp_session(make_token(alice))
    names = {t["name"] for t in session.rpc("tools/list")["result"]["tools"]}
    assert {"list_cases", "get_case"} <= names


def test_list_cases_is_limited_to_the_tokens_scope(
    mcp_session, make_token, make_case, alice
):
    make_case("A-1", alice.id)
    make_case("A-2", alice.id)
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    error, out = scoped.call("list_cases")
    assert not error
    assert [c["case_id"] for c in _result(out)] == ["A-1"]


def test_all_cases_token_sees_cases_created_after_minting(
    mcp_session, make_token, make_case, alice
):
    session = mcp_session(make_token(alice))
    assert _result(session.call("list_cases")[1]) == []
    make_case("LATE-1", alice.id)
    assert [c["case_id"] for c in _result(session.call("list_cases")[1])] == ["LATE-1"]


def test_a_token_never_sees_another_users_cases(
    mcp_session, make_token, make_case, alice, bob
):
    make_case("B-1", bob.id)
    session = mcp_session(make_token(alice))
    assert _result(session.call("list_cases")[1]) == []
    error, message = session.call("get_case", case_id="B-1")
    assert error and "outside this token's scope" in message


def test_get_case_reports_the_overview(mcp_session, make_token, make_case, alice):
    make_case("A-1", alice.id, title="Vogt v. Meier")
    error, out = mcp_session(make_token(alice)).call("get_case", case_id="A-1")
    assert not error
    assert out["case_id"] == "A-1" and out["title"] == "Vogt v. Meier"
    assert "documents" not in out


def test_read_only_token_still_reads(mcp_session, make_token, make_case, alice):
    make_case("A-1", alice.id)
    session = mcp_session(make_token(alice, access=ApiTokenAccess.READ))
    assert not session.call("get_case", case_id="A-1")[0]


def test_host_allowlist_is_optional_and_enforced_when_set(
    mcp_session, make_token, alice, monkeypatch
):
    import app.config

    session = mcp_session(make_token(alice))
    assert session.client.post(
        "/mcp", headers={**session.headers, "Host": "evil.example"}, json={}
    ).status_code not in (421,)
    monkeypatch.setattr(app.config, "MCP_ALLOWED_HOSTS", ["sanctuary.example.org"])
    blocked = session.client.post(
        "/mcp", headers={**session.headers, "Host": "evil.example"}, json={}
    )
    assert blocked.status_code == 421
    allowed = session.client.post(
        "/mcp",
        headers={**session.headers, "Host": "sanctuary.example.org:443"},
        json={},
    )
    assert allowed.status_code != 421


def test_mcp_and_oauth_routes_survive_the_default_rate_limiter(
    mcp_client, mcp_session, make_token, alice
):
    """SlowAPIMiddleware names the matched endpoint via ``__name__``, which the raw
    ASGI /mcp and OAuth endpoints lack: with the limiter on they all 500'd."""
    from app.core.rate_limit import limiter

    limiter.enabled = True  # the autouse fixture restores the previous value
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        assert mcp_client.get(path).status_code == 200, path
    # 401 from AuthGateMiddleware: the request got past slowapi to the auth layer.
    assert mcp_client.post("/mcp", json={}).status_code == 401
    # The OAuth POST endpoints (wrapped by _handler) reach their own handlers.
    assert mcp_client.post("/register", json={}).status_code == 400
    assert mcp_client.post("/token", data={}).status_code == 401
    assert mcp_client.post("/revoke", data={}).status_code == 400
    session = mcp_session(make_token(alice))
    assert "result" in session.rpc("tools/list")


def test_exempt_asgi_registers_the_key_slowapi_looks_up():
    from app.core.rate_limit import exempt_asgi, limiter

    class Endpoint:
        async def __call__(self, scope, receive, send): ...

    endpoint = exempt_asgi(Endpoint(), "probe")
    assert f"{endpoint.__module__}.{endpoint.__name__}" in limiter._exempt_routes
    assert endpoint.__module__ == "app.core.rate_limit"
