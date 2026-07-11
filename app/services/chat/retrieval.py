"""Retrieve the top-K documents for a case given a query string.

Uses passage-level (chunk) vector embeddings (`document_chunk_vectors`
sqlite-vec table, joined through `document_chunks`). Each hit surfaces the
chunk(s) that actually matched the query — the precise passage, not a
whole-document average — falling back to the document's static AI-curated
key_passages when vector retrieval is unavailable.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.database import Document, DocumentChunk

logger = logging.getLogger(__name__)

# A document's matching passage may not be its only high-ranked chunk, and
# several chunks from the same document can outrank a different document's
# best chunk — oversample so filtering to this case still yields k documents.
_CHUNK_OVERSAMPLE = 8
_MAX_PASSAGES_PER_DOC = 3


@dataclass
class RetrievalHit:
    doc_id: int
    case_id: str | None
    title: str
    key_passages: list[dict] = field(default_factory=list)
    significance_tier: str | None = None
    originator_type: str | None = None
    attributed_originator: str | None = None
    issued_date: str | None = None


def _to_hit(d: Document, key_passages: list[dict]) -> RetrievalHit:
    return RetrievalHit(
        doc_id=d.id,
        case_id=d.case_id,
        title=d.title or "Untitled",
        key_passages=key_passages,
        significance_tier=d.significance_tier.value if d.significance_tier else None,
        originator_type=d.originator_type.value if d.originator_type else None,
        attributed_originator=d.attributed_originator or d.sender,
        issued_date=d.issued_date.strftime("%Y-%m-%d") if d.issued_date else None,
    )


async def retrieve_top_docs(
    query: str, case_id: str, db: Session, k: int = 6, proceeding_id: int | None = None
) -> list[RetrievalHit]:
    """Return up to k RetrievalHits for this case, ranked by semantic similarity.

    Each hit's key_passages are the chunk(s) that actually matched the
    query — passage-level, not the document's static key_passages. Falls
    back to the most-recent documents (with their static key_passages) when
    embeddings are unavailable.
    """
    from app.services.ai_config import get_embed_config
    from app.services.ai_provider import embed_provider
    from app.services.embeddings import _serialize

    embed_provider.reload_from_db(db)
    cfg = get_embed_config(db)

    try:
        import httpx
        from sqlalchemy import text

        params = await embed_provider.get_embedding_params(cfg.embed_model, query)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                params["url"], json=params["json"], headers=params["headers"]
            )
            resp.raise_for_status()
            data = resp.json()
            embedding = data.get("embedding") or (
                data.get("data", [{}])[0].get("embedding") if data.get("data") else None
            )

        if not embedding or len(embedding) != cfg.embed_dim:
            raise ValueError("embedding dimension mismatch or empty")

        blob = _serialize(embedding)
        rows = db.execute(
            text(
                "SELECT chunk_id, distance FROM document_chunk_vectors "
                "WHERE embedding MATCH :blob ORDER BY distance LIMIT :k"
            ),
            {"blob": blob, "k": k * _CHUNK_OVERSAMPLE},
        ).fetchall()

        ranked_chunk_ids = [row[0] for row in rows]
        if not ranked_chunk_ids:
            raise ValueError("no chunk matches")

        chunk_query = (
            db.query(DocumentChunk)
            .join(Document)
            .filter(DocumentChunk.id.in_(ranked_chunk_ids), Document.case_id == case_id)
        )
        if proceeding_id:
            chunk_query = chunk_query.filter(Document.proceeding_id == proceeding_id)
        matched_chunks = chunk_query.all()
        if not matched_chunks:
            raise ValueError("no chunk matches in this case")

        chunk_rank = {cid: idx for idx, cid in enumerate(ranked_chunk_ids)}
        matched_chunks.sort(key=lambda c: chunk_rank.get(c.id, len(ranked_chunk_ids)))

        passages_by_doc: dict[int, list[DocumentChunk]] = defaultdict(list)
        doc_rank: dict[int, int] = {}
        for c in matched_chunks:
            passages_by_doc[c.document_id].append(c)
            doc_rank.setdefault(c.document_id, chunk_rank.get(c.id, 0))

        ranked_doc_ids = sorted(doc_rank, key=lambda did: doc_rank[did])[:k]

        docs_by_id = {
            d.id: d
            for d in db.query(Document).filter(Document.id.in_(ranked_doc_ids)).all()
        }

        return [
            _to_hit(
                docs_by_id[doc_id],
                [
                    {"text": c.text}
                    for c in passages_by_doc[doc_id][:_MAX_PASSAGES_PER_DOC]
                ],
            )
            for doc_id in ranked_doc_ids
            if doc_id in docs_by_id
        ]

    except Exception as e:
        logger.debug(f"Vector retrieval failed ({e}), falling back to recency")
        recency_query = db.query(Document).filter(Document.case_id == case_id)
        if proceeding_id:
            recency_query = recency_query.filter(
                Document.proceeding_id == proceeding_id
            )

        docs = recency_query.order_by(Document.issued_date.desc()).limit(k).all()
        return [_to_hit(d, d.key_passages or []) for d in docs]
