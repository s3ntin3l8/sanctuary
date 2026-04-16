import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.embeddings import generate_embedding


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_success(db_session, sample_document):
    mock_embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.json.return_value = {"embedding": mock_embedding}
    mock_response.raise_for_status = MagicMock()

    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("app.services.embeddings.SessionLocal") as mock_session_local,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_post.return_value = mock_response
        mock_session_local.return_value = db_session

        await generate_embedding(sample_document.id)

        db_session.refresh(sample_document)
        assert sample_document.content_embedding is not None
        assert json.loads(sample_document.content_embedding) == mock_embedding


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_failure(db_session, sample_document):
    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("app.services.embeddings.SessionLocal") as mock_session_local,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_post.side_effect = Exception("Ollama offline")
        mock_session_local.return_value = db_session

        # Should fail silently but not crash
        await generate_embedding(sample_document.id)

        db_session.refresh(sample_document)
        assert sample_document.content_embedding is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_no_doc(db_session):
    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("app.services.embeddings.SessionLocal") as mock_session_local,
    ):
        mock_session_local.return_value = db_session
        # Calling with non-existent ID
        await generate_embedding(9999)

        mock_post.assert_not_called()
