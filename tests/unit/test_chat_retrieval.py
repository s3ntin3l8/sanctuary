from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.database import Case, Document, DocumentChunk
from app.models.enums import CaseStatus, Jurisdiction
from app.services.chat.retrieval import retrieve_top_docs


def _seed_chunk(db_session, doc, chunk_index, chunk_text, vec):
    chunk = DocumentChunk(
        document_id=doc.id, chunk_index=chunk_index, text=chunk_text, embedding=vec
    )
    db_session.add(chunk)
    db_session.flush()
    return chunk


def _mock_embed_call(vec):
    resp = MagicMock()
    resp.json.return_value = {"embedding": vec}
    resp.raise_for_status = MagicMock()
    return (
        patch(
            "app.services.ai_provider.embed_provider.get_embedding_params",
            new_callable=AsyncMock,
            return_value={"url": "http://x", "json": {}, "headers": {}},
        ),
        patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=resp),
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_retrieve_top_docs_surfaces_matched_chunk_text(db_session, sample_case):
    from app.config import AI_EMBED_DIM

    doc = Document(
        title="Ruling",
        content="x",
        case_id=sample_case.id,
        key_passages=[{"text": "static AI-curated passage"}],
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    vec = [0.1] * AI_EMBED_DIM
    _seed_chunk(db_session, doc, 0, "the actual matched passage", vec)
    db_session.commit()

    p1, p2 = _mock_embed_call(vec)
    with p1, p2:
        hits = await retrieve_top_docs("query", sample_case.id, db_session, k=6)

    assert len(hits) == 1
    assert hits[0].doc_id == doc.id
    # Passage-level: the matched chunk text, not the static key_passages.
    assert hits[0].key_passages == [{"text": "the actual matched passage"}]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_retrieve_top_docs_groups_multiple_chunks_per_doc(
    db_session, sample_case, caplog
):
    from app.config import AI_EMBED_DIM

    doc = Document(title="Multi", content="x", case_id=sample_case.id)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    vec = [0.1] * AI_EMBED_DIM
    _seed_chunk(db_session, doc, 0, "chunk one", vec)
    _seed_chunk(db_session, doc, 1, "chunk two", vec)
    db_session.commit()

    p1, p2 = _mock_embed_call(vec)
    with p1, p2:
        hits = await retrieve_top_docs("query", sample_case.id, db_session, k=6)

    # One hit per document, not one per chunk.
    assert len(hits) == 1
    assert hits[0].doc_id == doc.id
    assert len(hits[0].key_passages) == 2
    # Distinguish "KNN succeeded" from "silently fell back to recency" — the
    # doc has no static key_passages, so a fallback would ALSO produce an
    # empty/short list here and this assertion alone wouldn't catch it.
    assert "falling back to recency" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_retrieve_top_docs_excludes_other_case_chunks(
    db_session, sample_case, caplog
):
    from app.config import AI_EMBED_DIM

    other_case = Case(
        id="TEST-002",
        title="Other",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(other_case)
    db_session.commit()

    other_doc = Document(title="Other case doc", content="x", case_id=other_case.id)
    db_session.add(other_doc)
    db_session.commit()
    db_session.refresh(other_doc)

    vec = [0.1] * AI_EMBED_DIM
    _seed_chunk(db_session, other_doc, 0, "belongs to a different case", vec)
    db_session.commit()

    p1, p2 = _mock_embed_call(vec)
    with p1, p2:
        hits = await retrieve_top_docs("query", sample_case.id, db_session, k=6)

    # The only matching chunk belongs to a different case, and sample_case
    # has no documents at all — nothing to fall back to either, so a silent
    # fallback would ALSO produce [] here. This case IS expected to hit the
    # fallback (KNN matched a chunk, but the case-scoping filter rejected it —
    # the code's own "no chunk matches in this case" ValueError, one of the
    # intentional degrade conditions) — confirm it's *that* reason, not an
    # unrelated provider/pgvector failure masquerading as the same empty result.
    assert hits == []
    assert "no chunk matches in this case" in caplog.text


@pytest.mark.asyncio
@pytest.mark.unit
async def test_retrieve_top_docs_falls_back_to_recency_on_provider_failure(
    db_session, sample_case
):
    doc = Document(
        title="Recent",
        content="x",
        case_id=sample_case.id,
        key_passages=[{"text": "static passage"}],
    )
    db_session.add(doc)
    db_session.commit()

    with patch(
        "app.services.ai_provider.embed_provider.get_embedding_params",
        new_callable=AsyncMock,
        side_effect=RuntimeError("provider down"),
    ):
        hits = await retrieve_top_docs("query", sample_case.id, db_session, k=6)

    assert len(hits) == 1
    assert hits[0].doc_id == doc.id
    # Fallback uses the document's static key_passages, not a chunk match.
    assert hits[0].key_passages == [{"text": "static passage"}]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_retrieve_top_docs_propagates_unexpected_exception(
    db_session, sample_case
):
    """The narrowed except only degrades on known conditions (provider down,
    dim mismatch, no matches, pgvector failure). An unrelated bug — a
    TypeError here — must propagate instead of silently becoming a
    recency-fallback result, or a real regression would be indistinguishable
    from "no matches"."""
    with patch(
        "app.services.ai_provider.embed_provider.get_embedding_params",
        new_callable=AsyncMock,
        side_effect=TypeError("simulated unexpected bug"),
    ):
        with pytest.raises(TypeError, match="simulated unexpected bug"):
            await retrieve_top_docs("query", sample_case.id, db_session, k=6)
