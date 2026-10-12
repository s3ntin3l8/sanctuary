"""Fixtures for driving the MCP endpoint through the full HTTP stack."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case
from app.models.enums import ApiTokenAccess, CaseStatus, Jurisdiction
from app.services import api_token_service, auth_service

PASSWORD = "password123"  # pragma: allowlist secret


@pytest.fixture
def mcp_client():
    """TestClient with the app lifespan running (starts the MCP session manager)."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def alice(db_session):
    user = auth_service.create_user(
        db_session, email="alice@example.com", password=PASSWORD
    )
    db_session.commit()
    return user


@pytest.fixture
def bob(db_session):
    user = auth_service.create_user(
        db_session, email="bob@example.com", password=PASSWORD
    )
    db_session.commit()
    return user


@pytest.fixture
def make_case(db_session):
    def _make(case_id: str, owner_id: int, title: str | None = None) -> Case:
        case = Case(
            id=case_id,
            title=title or f"Case {case_id}",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
            owner_id=owner_id,
        )
        db_session.add(case)
        db_session.commit()
        return case

    return _make


@pytest.fixture
def make_token(db_session):
    def _make(
        user, *, access=ApiTokenAccess.READ, all_cases=True, case_ids=None
    ) -> str:
        _, secret = api_token_service.mint(
            db_session,
            user,
            name="agent",
            access=access,
            all_cases=all_cases,
            case_ids=case_ids,
        )
        db_session.commit()
        return secret

    return _make


class McpSession:
    """Minimal JSON-RPC caller for the stateless streamable-HTTP endpoint."""

    def __init__(self, client: TestClient, secret: str):
        self.client = client
        self.headers = {
            "Authorization": f"Bearer {secret}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._id = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None):
        self._id += 1
        resp = self.client.post(
            "/mcp",
            headers=self.headers,
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self._id,
                    "method": method,
                    "params": params or {},
                }
            ),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def call(self, tool: str, **arguments):
        """Return (is_error, structured_or_text) of a tools/call."""
        body = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        result = body["result"]
        if result.get("isError"):
            return True, result["content"][0]["text"]
        if result.get("structuredContent") is not None:
            return False, result["structuredContent"]
        return False, json.loads(result["content"][0]["text"])

    def read_resource(self, uri: str):
        """Return (is_error, text_or_message) of a resources/read."""
        body = self.rpc("resources/read", {"uri": uri})
        if "error" in body:
            return True, body["error"]["message"]
        return False, body["result"]["contents"][0]["text"]

    def get_prompt(self, name: str, **arguments):
        """Return (is_error, joined_message_text_or_error) of a prompts/get."""
        body = self.rpc(
            "prompts/get",
            {"name": name, "arguments": {k: str(v) for k, v in arguments.items()}},
        )
        if "error" in body:
            return True, body["error"]["message"]
        messages = body["result"]["messages"]
        return False, "\n".join(m["content"]["text"] for m in messages)


@pytest.fixture
def mcp_session(mcp_client):
    def _open(secret: str) -> McpSession:
        return McpSession(mcp_client, secret)

    return _open


@pytest.fixture(autouse=True)
def _reset_ip_limits():
    """The OAuth endpoints' per-IP budgets are process-global; tests share an IP."""
    from app.core import rate_limit

    rate_limit._ip_limiter.storage.reset()
