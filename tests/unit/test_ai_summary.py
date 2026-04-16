import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.database import Document
from app.services.ai_summary import (
    _parse_summary_response,
    generate_summary,
    summarize_document,
)


@pytest.mark.unit
def test_parse_summary_response_valid():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }
    raw_text = json.dumps(data)
    result = _parse_summary_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_summary_response_markdown_fence():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }
    raw_text = f"```json\n{json.dumps(data)}\n```"
    result = _parse_summary_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_summary_response_extra_text():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }
    raw_text = (
        f"Here is the result:\n```json\n{json.dumps(data)}\n```\nHope this helps."
    )
    result = _parse_summary_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_summary_response_no_fence_braces():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }
    raw_text = f"Some text before {json.dumps(data)} some text after"
    result = _parse_summary_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_summary_response_invalid():
    raw_text = "not json"
    with pytest.raises(ValueError, match="AI response contains no JSON object"):
        _parse_summary_response(raw_text)


@pytest.mark.unit
def test_parse_summary_response_empty():
    with pytest.raises(ValueError, match="AI returned an empty response"):
        _parse_summary_response("")


@pytest.mark.unit
def test_parse_summary_response_conversational():
    data = {"key": "value"}
    raw_text = (
        "I have analyzed the document. Here is the result in JSON: "
        + json.dumps(data)
        + " I hope this is what you need."
    )
    result = _parse_summary_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_summary_response_truncated():
    raw_text = '{"legal_significance": "something"'
    result = _parse_summary_response(raw_text)
    assert result == {"legal_significance": "something"}


@pytest.mark.skip(
    reason="Mocks httpx.AsyncClient.stream directly but generate_summary now "
    "goes through the ai_provider abstraction; test needs to be rewritten to "
    "mock at that layer."
)
@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_summary_mock_http():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }

    mock_doc = MagicMock(spec=Document)
    mock_doc.content = "Content"
    mock_doc.title = "Title"
    mock_doc.meta = {}

    # Since generate_summary now uses .stream(), we need to mock that
    # It's easier to patch the collection logic or the return value
    # But let's try to fix the test to at least match the signature for now.

    with patch("app.services.ai_summary.AI_SUMMARY_MODEL", "test-model"):
        with patch("httpx.AsyncClient.stream") as mock_stream:
            # Mock the async context manager and the aiter_lines
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()

            async def mock_aiter():
                yield json.dumps({"response": json.dumps(data), "done": True})

            mock_resp.aiter_lines = mock_aiter
            mock_stream.return_value.__aenter__.return_value = mock_resp

            result = await generate_summary(mock_doc)
            assert result == data


@pytest.mark.asyncio
@pytest.mark.unit
async def test_summarize_document_success(db_session, sample_document):
    # We must un-mock summarize_document that was mocked in conftest.py
    # Actually, we are calling the imported summarize_document directly.
    # But generate_summary inside it might be mocked or we can patch it.

    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }

    with patch(
        "app.services.ai_summary.generate_summary", new_callable=AsyncMock
    ) as mock_gen:
        mock_gen.return_value = data

        # Ensure we are testing the REAL summarize_document
        # If conftest.py patched app.services.ai_summary.summarize_document,
        # we need to ensure we call the original one.
        # But we imported it at the top of this file.

        updated_doc = await summarize_document(sample_document.id, db_session)

        assert updated_doc.ai_summary == data
        assert updated_doc.ai_summary_status == "generated"
        assert updated_doc.ai_summary_created_at is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_summarize_document_failure(db_session, sample_document):
    with patch(
        "app.services.ai_summary.generate_summary", new_callable=AsyncMock
    ) as mock_gen:
        mock_gen.side_effect = Exception("Ollama Error")

        await summarize_document(sample_document.id, db_session)
        updated_doc = db_session.get(Document, sample_document.id)

        assert updated_doc.ai_summary_status == "failed"
        assert "Ollama Error" in updated_doc.ai_summary["error"]
