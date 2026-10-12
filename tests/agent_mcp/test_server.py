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
