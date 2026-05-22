"""Wave 2B: claim-level embeddings for semantic dedup and pre-extraction
context. Mirrors the document-level pipeline in app/services/embeddings.py.

The claim_vectors vec0 virtual table holds one row per claim; embeddings
are written on insert and rewritten on text update. Similarity queries
power the dedup judge's top-K nearest lookup and the extractor's
pre-extraction context.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.database import Claim
from app.services.ai_config import get_embed_config
from app.services.ai_provider import embed_provider
from app.services.intelligence._ai_call import _parse_litellm_error_summary

logger = logging.getLogger(__name__)


def _serialize(vec: list[float]) -> bytes:
    """sqlite-vec f32 blob format."""
    from sqlite_vec import serialize_float32

    return serialize_float32(vec)


async def embed_claim_text(claim_text: str, db: Session) -> list[float] | None:
    """Compute the embedding for a claim's text. Returns None on any failure
    (logged at ERROR with the litellm body summary when available). Caller
    decides whether to retry / skip — upsert_claim_embedding additionally
    records the failure on the Claim row via embedding_failed_at."""
    embed_provider.reload_from_db(db)
    cfg = get_embed_config(db)
    try:
        params = await embed_provider.get_embedding_params(cfg.embed_model, claim_text)
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                params["url"], json=params["json"], headers=params["headers"]
            )
            if not response.is_success:
                # Surface the litellm body — embedding failures were previously
                # swallowed at WARNING level, leaving 60+ silent failures/day
                # invisible in normal log monitoring. Log at ERROR with the
                # parsed body summary so they're discoverable in celery.log.
                summary = _parse_litellm_error_summary(response.content) or ""
                logger.error(
                    "claim embedding HTTP %s: %s",
                    response.status_code,
                    summary[:200] or response.text[:200],
                )
                return None
            data = response.json()
        embedding = data.get("embedding") or (
            data.get("data", [{}])[0].get("embedding") if data.get("data") else None
        )
        if not embedding or len(embedding) != cfg.embed_dim:
            logger.error(
                "claim embedding rejected: returned %s dims, expected %s",
                len(embedding) if embedding else 0,
                cfg.embed_dim,
            )
            return None
        return embedding
    except Exception as exc:  # noqa: BLE001
        logger.error("claim embedding failed: %s", exc)
        return None


async def upsert_claim_embedding(claim_id: int, db: Session) -> bool:
    """Embed `claim_id`'s text and write to claim_vectors. Idempotent
    (DELETE + INSERT — vec0 doesn't honor ON CONFLICT).

    Records failure on the Claim via embedding_failed_at so the system has
    persistent signal — a periodic maintenance task can find these and
    re-attempt later. Clears the timestamp on success so a recovered claim
    looks healthy again."""
    claim = db.get(Claim, claim_id)
    if not claim or not claim.claim_text:
        return False
    embedding = await embed_claim_text(claim.claim_text, db)
    if embedding is None:
        claim.embedding_failed_at = datetime.now(UTC)
        db.commit()
        return False
    blob = _serialize(embedding)
    db.execute(
        text("DELETE FROM claim_vectors WHERE claim_id = :cid"),
        {"cid": claim_id},
    )
    db.execute(
        text(
            "INSERT INTO claim_vectors(claim_id, embedding) VALUES (:cid, :embedding)"
        ),
        {"cid": claim_id, "embedding": blob},
    )
    claim.embedding_failed_at = None
    db.commit()
    return True


async def nearest_claims(
    query_text: str,
    db: Session,
    *,
    k: int = 5,
    case_id: str | None = None,
    exclude_claim_id: int | None = None,
) -> list[tuple[int, float]]:
    """Return up to `k` nearest existing claims to `query_text` as
    (claim_id, distance) pairs, sorted ascending by distance.

    If `case_id` is provided, restricts to claims with at least one
    ClaimEvidence row in that case (i.e. case-scoped neighbor search).
    Without `case_id` the search is across the whole global pool — the
    cross-case flow Wave 2A enables.
    """
    embedding = await embed_claim_text(query_text, db)
    if embedding is None:
        return []
    blob = _serialize(embedding)

    # vec0 KNN is fastest as a standalone MATCH query, then we filter the
    # results in Python (or via a follow-up join). We over-fetch a bit for
    # case-scoping headroom.
    fetch_k = k * 4 if case_id else k
    rows = db.execute(
        text(
            "SELECT claim_id, distance FROM claim_vectors "
            "WHERE embedding MATCH :blob "
            "ORDER BY distance LIMIT :k"
        ),
        {"blob": blob, "k": fetch_k},
    ).fetchall()

    if exclude_claim_id is not None:
        rows = [r for r in rows if r[0] != exclude_claim_id]

    if not case_id:
        return [(r[0], r[1]) for r in rows[:k]]

    # Filter to claims with evidence in this case.
    candidate_ids = [r[0] for r in rows]
    if not candidate_ids:
        return []
    in_case_ids = {
        cid
        for (cid,) in db.execute(
            text(
                "SELECT DISTINCT ce.claim_id "
                "FROM claim_evidence ce JOIN documents d ON d.id = ce.document_id "
                "WHERE ce.claim_id IN :ids AND d.case_id = :cid"
            ).bindparams(
                _expanding_int_list("ids"),
            ),
            {"ids": candidate_ids, "cid": case_id},
        ).fetchall()
    }
    return [(cid, dist) for cid, dist in rows if cid in in_case_ids][:k]


def _expanding_int_list(name: str):
    """Helper: SQLAlchemy bindparam for an IN clause."""
    from sqlalchemy import bindparam

    return bindparam(name, expanding=True)
