"""Pin two safety nets around `AI_EMBED_DIM`:

1. When a provider returns an embedding whose length doesn't match
   `cfg.embed_dim`, `generate_embedding` must raise. The Celery task wrapper
   then marks EMBEDDINGS=FAILED — without that signal, the doc was previously
   marked COMPLETED with no vector, so search silently missed it.

2. `verify_vec0_dim()` reads the existing `document_chunk_vectors` schema,
   parses its declared dimension, and reports whether it matches `AI_EMBED_DIM`.
   The startup hook uses this to fail loudly when the env var was changed
   without recreating the vec0 table.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dim_mismatch_raises(db_session):
    """Provider returns 100-dim vector but config expects 768 → ValueError."""
    from app.models.database import Case, Document, IngestBatch
    from app.models.enums import IngestBatchSourceType
    from app.services import embeddings as emb_module
    from app.services.ai_provider import ProviderType

    case = Case(id="EMB-DIM-1", title="t")
    db_session.add(case)
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
    db_session.add(batch)
    db_session.commit()
    doc = Document(
        title="t",
        content="some content",
        ingest_batch_id=batch.id,
        case_id=case.id,
    )
    db_session.add(doc)
    db_session.commit()

    fake_response = AsyncMock()
    fake_response.json = lambda: {"embedding": [0.1] * 100}  # wrong dim
    fake_response.raise_for_status = lambda: None

    with (
        patch.object(emb_module, "SessionLocal", lambda: db_session),
        patch.object(
            emb_module.embed_provider, "get_embedding_params", new_callable=AsyncMock
        ) as mock_params,
        patch.object(
            emb_module.embed_provider, "get_type", new_callable=AsyncMock
        ) as mock_get_type,
        patch("httpx.AsyncClient") as mock_client,
    ):
        mock_params.return_value = {"url": "x", "json": {}, "headers": {}}
        mock_get_type.return_value = ProviderType.OLLAMA
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=fake_response
        )

        with pytest.raises(ValueError, match="dim mismatch"):
            await emb_module.generate_embedding(doc.id)


@pytest.mark.unit
def test_verify_vec0_dim_match(db_session):
    """When the vec0 table dimension matches AI_EMBED_DIM, return (True, dim)."""
    from app.services.embeddings import verify_vec0_dim

    ok, dim = verify_vec0_dim(db_session, expected_dim=768)
    assert ok is True
    assert dim == 768


@pytest.mark.unit
def test_verify_vec0_dim_mismatch(db_session):
    """When the env var differs from the schema, return (False, actual_dim)."""
    from app.services.embeddings import verify_vec0_dim

    ok, dim = verify_vec0_dim(db_session, expected_dim=512)
    assert ok is False
    assert dim == 768
