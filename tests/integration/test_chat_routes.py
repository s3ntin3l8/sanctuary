import json
import pytest
from unittest.mock import patch, AsyncMock

@pytest.mark.integration
def test_chat_conversations_create_and_get(app_client, sample_case):
    # Create/get conversation
    resp = app_client.post("/api/chat/conversations", json={
        "scope_type": "case",
        "scope_id": sample_case.id
    })
    assert resp.status_code == 200
    data = resp.json()
    conv_id = data["id"]
    assert data["scope_id"] == sample_case.id
    assert data["messages"] == []

    # Get specific conversation
    resp = app_client.get(f"/api/chat/conversations/{conv_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == conv_id

    # List conversations for scope
    resp = app_client.get(f"/api/chat/conversations?scope_type=case&scope_id={sample_case.id}")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert resp.json()[0]["id"] == conv_id

@pytest.mark.integration
def test_chat_update_title(app_client, sample_case):
    resp = app_client.post("/api/chat/conversations", json={
        "scope_type": "case",
        "scope_id": sample_case.id
    })
    conv_id = resp.json()["id"]

    resp = app_client.post(f"/api/chat/conversations/{conv_id}/title", json={
        "title": "Updated Title"
    })
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated Title"

    resp = app_client.get(f"/api/chat/conversations/{conv_id}")
    assert resp.json()["title"] == "Updated Title"

@pytest.mark.integration
def test_chat_stream_message(app_client, sample_case):
    resp = app_client.post("/api/chat/conversations", json={
        "scope_type": "case",
        "scope_id": sample_case.id
    })
    conv_id = resp.json()["id"]

    # Mock the stream_answer generator
    async def mock_stream(*args, **kwargs):
        yield 'data: {"type": "token", "t": "Hello"}\n\n'
        yield 'data: {"type": "token", "t": " World"}\n\n'
        yield 'data: {"type": "citations", "docs": []}\n\n'
        yield 'data: {"type": "done"}\n\n'

    with patch("app.api.chat.stream_answer", side_effect=mock_stream):
        resp = app_client.post(f"/api/chat/conversations/{conv_id}/messages", json={
            "content": "Hi there"
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        
        # Verify content
        lines = [line for line in resp.iter_lines() if line]
        assert len(lines) == 4
        assert '{"type": "token", "t": "Hello"}' in lines[0]
        assert '{"type": "done"}' in lines[3]
