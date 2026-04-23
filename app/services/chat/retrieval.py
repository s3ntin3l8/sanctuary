"""Retrieve the top-K documents for a case given a query string.

Uses document-level vector embeddings (document_vectors sqlite-vec table).
The retrieval function is the seam for future chunk-level retrieval.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.database import Document

logger = logging.getLogger(__name__)


@dataclass
class RetrievalHit:
    doc_id: int
    case_id: str | None
    title: str
    key_passages: list[dict] = field(default_factory=list)
    significance_tier: str | None = None


async def retrieve_top_docs(
    query: str, case_id: str, db: Session, k: int = 6
) -> list[RetrievalHit]:
    """Return up to k RetrievalHits for this case, ranked by semantic similarity.

    Falls back to the most-recent documents when embeddings are unavailable.
    """
    from app.services.ai_config import get_effective_config
    from app.services.ai_provider import ai_provider
    from app.services.embeddings import _serialize

    cfg = get_effective_config(db)

    try:
        import httpx
        from sqlalchemy import text

        params = await ai_provider.get_embedding_params(cfg.embed_model, query)
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
                "SELECT document_id, distance FROM document_vectors "
                "WHERE embedding MATCH :blob ORDER BY distance LIMIT :k"
            ),
            {"blob": blob, "k": k * 3},
        ).fetchall()

        ranked_ids = [row[0] for row in rows]
        docs = (
            db.query(Document)
            .filter(Document.id.in_(ranked_ids), Document.case_id == case_id)
            .all()
        )
        id_order = {doc_id: idx for idx, doc_id in enumerate(ranked_ids)}
        docs.sort(key=lambda d: id_order.get(d.id, len(ranked_ids)))
        docs = docs[:k]

    except Exception as e:
        logger.debug(f"Vector retrieval failed ({e}), falling back to recency")
        docs = (
            db.query(Document)
            .filter(Document.case_id == case_id)
            .order_by(Document.received_date.desc())
            .limit(k)
            .all()
        )

    return [
        RetrievalHit(
            doc_id=d.id,
            case_id=d.case_id,
            title=d.title or "Untitled",
            key_passages=d.key_passages or [],
            significance_tier=d.significance_tier.value
            if d.significance_tier
            else None,
        )
        for d in docs
    ]
