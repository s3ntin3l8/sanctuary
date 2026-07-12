"""Regression tests for stream_answer's error handling.

Covers py/stack-trace-exposure: a raw exception from the AI provider stream
must not reach the SSE token the browser renders — only a generic message,
with the real detail logged server-side.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.models.database import Document
from app.models.enums import OriginatorType
from app.repositories.chat import ChatRepository
from app.services.ai_provider import ProviderType, chat_provider
from app.services.chat.chat_service import stream_answer


@pytest.mark.unit
async def test_stream_answer_generic_error_yields_generic_token(
    db_session, monkeypatch, sample_case
):
    doc = Document(
        title="Test Doc",
        content="Some content for the chat prompt.",
        case_id=sample_case.id,
        originator_type=OriginatorType.OPPOSING,
        sender="opposing@example.com",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    repo = ChatRepository(db_session)
    conv = repo.get_or_create(scope_type="document", scope_id=str(doc.id))

    monkeypatch.setattr(chat_provider, "reload_from_db", lambda db: None)
    monkeypatch.setattr(
        chat_provider, "get_type", AsyncMock(return_value=ProviderType.OLLAMA)
    )
    monkeypatch.setattr(
        chat_provider,
        "get_generate_params",
        AsyncMock(
            return_value={"url": "http://fake/api/generate", "json": {}, "headers": {}}
        ),
    )

    def _boom_stream(self, *args, **kwargs):
        raise RuntimeError("connection refused to 10.0.0.5:11434 (internal detail)")

    monkeypatch.setattr(httpx.AsyncClient, "stream", _boom_stream)

    tokens = [chunk async for chunk in stream_answer(conv, "hi", db_session)]
    joined = "".join(tokens)

    assert "Stream error" in joined
    assert "see server log" in joined
    assert "10.0.0.5" not in joined
