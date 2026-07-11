import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from app.config import AI_EMBED_DIM
from app.models.database import Document, DocumentChunk
from app.services.embeddings import (
    _chunks_to_embed,
    _serialize,
    generate_embedding,
    nearest_chunks,
    nearest_document_ids,
)


def test_serialize_roundtrip():
    vec = [0.1, 0.2, 0.3, 0.4]
    blob = _serialize(vec)
    assert isinstance(blob, bytes)
    assert len(blob) == 4 * len(vec)
    recovered = list(struct.unpack(f"{len(vec)}f", blob))
    assert all(abs(a - b) < 1e-6 for a, b in zip(recovered, vec, strict=False))


def test_chunks_to_embed_uses_doc_meta_chunks():
    doc = MagicMock()
    doc.meta = {"chunks": [{"text": "  first section  "}, {"text": "second section"}]}
    doc.content = "unused when chunks are present"
    assert _chunks_to_embed(doc) == ["first section", "second section"]


def test_chunks_to_embed_skips_blank_chunks():
    doc = MagicMock()
    doc.meta = {"chunks": [{"text": "   "}, {"text": "real text"}]}
    doc.content = ""
    assert _chunks_to_embed(doc) == ["real text"]


def test_chunks_to_embed_falls_back_to_content_windows_when_no_chunks():
    doc = MagicMock()
    doc.meta = None
    doc.content = "x" * 9000
    chunks = _chunks_to_embed(doc)
    assert len(chunks) == 3  # 9000 chars / 4000-char windows
    assert sum(len(c) for c in chunks) == 9000


def _mock_embedding_response(embedding: list[float]):
    resp = MagicMock()
    resp.json.return_value = {"embedding": embedding}
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_success(db_session, sample_document):
    mock_embedding = [0.1] * AI_EMBED_DIM

    with (
        patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            return_value=_mock_embedding_response(mock_embedding),
        ),
        patch("app.services.embeddings.SessionLocal", lambda: db_session),
    ):
        await generate_embedding(sample_document.id)

    chunks = (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == sample_document.id)
        .all()
    )
    assert len(chunks) == 1
    assert chunks[0].text == sample_document.content

    row = db_session.execute(
        text("SELECT embedding FROM document_chunk_vectors WHERE chunk_id = :cid"),
        {"cid": chunks[0].id},
    ).fetchone()
    assert row is not None
    assert len(row[0]) == 4 * AI_EMBED_DIM


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_wrong_dim_raises(db_session, sample_document):
    """Embeddings with wrong dimension raise so the task can mark FAILED,
    and nothing is left committed for this document."""
    mock_embedding = [0.1] * 3  # wrong dim

    with (
        patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            return_value=_mock_embedding_response(mock_embedding),
        ),
        patch("app.services.embeddings.SessionLocal", lambda: db_session),
    ):
        with pytest.raises(ValueError, match="dim mismatch"):
            await generate_embedding(sample_document.id)

    chunks = (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == sample_document.id)
        .all()
    )
    assert chunks == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_failure_propagates(db_session, sample_document):
    """Provider failures propagate so the task can mark FAILED and retry."""
    with (
        patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            side_effect=Exception("Ollama offline"),
        ),
        patch("app.services.embeddings.SessionLocal", lambda: db_session),
    ):
        with pytest.raises(Exception, match="Ollama offline"):
            await generate_embedding(sample_document.id)

    chunks = (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == sample_document.id)
        .all()
    )
    assert chunks == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_embedding_is_idempotent_on_retry(db_session, sample_document):
    """Re-running generate_embedding for a doc that already has chunks must
    not raise UNIQUE on document_chunk_vectors, and must not leave
    duplicate/orphaned rows behind — vec0 ignores INSERT OR REPLACE so the
    code must DELETE before INSERT.
    """
    mock_embedding = [0.1] * AI_EMBED_DIM

    with (
        patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            return_value=_mock_embedding_response(mock_embedding),
        ),
        patch("app.services.embeddings.SessionLocal", lambda: db_session),
    ):
        # Two calls back-to-back — second would raise UNIQUE if the code
        # weren't doing DELETE-then-INSERT.
        await generate_embedding(sample_document.id)
        await generate_embedding(sample_document.id)

    chunks = (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == sample_document.id)
        .all()
    )
    assert len(chunks) == 1  # not 2 — no duplicate chunk rows

    vector_count = db_session.execute(
        text("SELECT count(*) FROM document_chunk_vectors")
    ).scalar()
    assert vector_count == 1  # no orphaned vec0 row from the first embed


@pytest.mark.unit
def test_nearest_chunks_empty_query_short_circuits(db_session):
    """No query text → no provider call, empty result."""
    with patch(
        "app.services.ai_provider.embed_provider.get_embedding_params",
        new_callable=AsyncMock,
    ) as mock_params:
        assert nearest_chunks("", db_session, k=5) == []
    mock_params.assert_not_called()


@pytest.mark.unit
def test_nearest_document_ids_empty_query_short_circuits(db_session):
    """No query text → no provider call, empty result."""
    with patch(
        "app.services.ai_provider.embed_provider.get_embedding_params",
        new_callable=AsyncMock,
    ) as mock_params:
        assert nearest_document_ids("", db_session, k=5) == []
    mock_params.assert_not_called()


@pytest.mark.unit
def test_nearest_document_ids_returns_match(db_session, sample_case):
    """Success path: embed the query, vec0 MATCH returns the doc owning the
    chunk whose stored vector is identical (distance 0)."""
    doc = Document(title="Vektor", content="x", case_id=sample_case.id)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    chunk = DocumentChunk(document_id=doc.id, chunk_index=0, text="x")
    db_session.add(chunk)
    db_session.flush()

    vec = [0.1] * AI_EMBED_DIM
    db_session.execute(
        text(
            "INSERT INTO document_chunk_vectors(chunk_id, embedding) VALUES (:id, :e)"
        ),
        {"id": chunk.id, "e": _serialize(vec)},
    )
    db_session.commit()

    resp = MagicMock()
    resp.json.return_value = {"embedding": vec}
    resp.raise_for_status = MagicMock()

    with (
        patch(
            "app.services.ai_provider.embed_provider.get_embedding_params",
            new_callable=AsyncMock,
            return_value={"url": "http://x", "json": {}, "headers": {}},
        ),
        patch("httpx.Client.post", return_value=resp),
    ):
        ids = nearest_document_ids("anything", db_session, k=5)

    assert doc.id in ids


@pytest.mark.unit
def test_nearest_document_ids_dedupes_multiple_chunk_hits_from_same_doc(
    db_session, sample_case
):
    """A document with several matching chunks appears once in the ranked
    document id list, not once per chunk."""
    doc = Document(title="Multi-chunk", content="x", case_id=sample_case.id)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    vec = [0.1] * AI_EMBED_DIM
    for idx in range(3):
        chunk = DocumentChunk(document_id=doc.id, chunk_index=idx, text=f"chunk {idx}")
        db_session.add(chunk)
        db_session.flush()
        db_session.execute(
            text(
                "INSERT INTO document_chunk_vectors(chunk_id, embedding) VALUES (:id, :e)"
            ),
            {"id": chunk.id, "e": _serialize(vec)},
        )
    db_session.commit()

    resp = MagicMock()
    resp.json.return_value = {"embedding": vec}
    resp.raise_for_status = MagicMock()

    with (
        patch(
            "app.services.ai_provider.embed_provider.get_embedding_params",
            new_callable=AsyncMock,
            return_value={"url": "http://x", "json": {}, "headers": {}},
        ),
        patch("httpx.Client.post", return_value=resp),
    ):
        ids = nearest_document_ids("anything", db_session, k=5)

    assert ids.count(doc.id) == 1


@pytest.mark.unit
def test_nearest_document_ids_dim_mismatch_returns_empty(db_session):
    """A wrong-dimension embedding is rejected (never reaches the vec query)."""
    resp = MagicMock()
    resp.json.return_value = {"embedding": [0.1, 0.2, 0.3]}  # wrong dim
    resp.raise_for_status = MagicMock()

    with (
        patch(
            "app.services.ai_provider.embed_provider.get_embedding_params",
            new_callable=AsyncMock,
            return_value={"url": "http://x", "json": {}, "headers": {}},
        ),
        patch("httpx.Client.post", return_value=resp),
    ):
        assert nearest_document_ids("anything", db_session, k=5) == []


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
