import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import AI_EMBED_DIM
from app.services.embeddings import _serialize, generate_embedding


def test_serialize_roundtrip():
    vec = [0.1, 0.2, 0.3, 0.4]
    blob = _serialize(vec)
    assert isinstance(blob, bytes)
    assert len(blob) == 4 * len(vec)
    recovered = list(struct.unpack(f"{len(vec)}f", blob))
    assert all(abs(a - b) < 1e-6 for a, b in zip(recovered, vec, strict=False))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_success(db_session, sample_document):
    mock_embedding = [0.1] * AI_EMBED_DIM
    mock_response = MagicMock()
    mock_response.json.return_value = {"embedding": mock_embedding}
    mock_response.raise_for_status = MagicMock()

    executed = []

    def capture_execute(stmt, params=None):
        executed.append((str(stmt), params))
        return MagicMock()

    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("app.services.embeddings.SessionLocal") as mock_session_local,
    ):
        mock_post.return_value = mock_response
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = (
            sample_document
        )
        mock_db.execute.side_effect = capture_execute
        mock_session_local.return_value = mock_db

        await generate_embedding(sample_document.id)

    assert len(executed) == 1
    sql, params = executed[0]
    assert "document_vectors" in sql
    assert params["doc_id"] == sample_document.id
    assert isinstance(params["embedding"], bytes)
    assert len(params["embedding"]) == 4 * AI_EMBED_DIM


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_wrong_dim_skipped(db_session, sample_document):
    """Embeddings with wrong dimension are silently skipped."""
    mock_embedding = [0.1] * 3  # wrong dim
    mock_response = MagicMock()
    mock_response.json.return_value = {"embedding": mock_embedding}
    mock_response.raise_for_status = MagicMock()

    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("app.services.embeddings.SessionLocal") as mock_session_local,
    ):
        mock_post.return_value = mock_response
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = (
            sample_document
        )
        mock_session_local.return_value = mock_db

        await generate_embedding(sample_document.id)

    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_failure(db_session, sample_document):
    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("app.services.embeddings.SessionLocal") as mock_session_local,
    ):
        mock_post.side_effect = Exception("Ollama offline")
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = (
            sample_document
        )
        mock_session_local.return_value = mock_db

        await generate_embedding(sample_document.id)

    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_no_doc(db_session):
    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("app.services.embeddings.SessionLocal") as mock_session_local,
    ):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_session_local.return_value = mock_db

        await generate_embedding(9999)

    mock_post.assert_not_called()
