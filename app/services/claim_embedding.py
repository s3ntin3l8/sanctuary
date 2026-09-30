"""Wave 2B: claim-level embeddings for semantic dedup and pre-extraction
context. Mirrors the document-level pipeline in app/services/embeddings.py.

Claim.embedding (pgvector, HNSW-indexed) holds one vector per claim; it's
written on insert and rewritten on text update. Similarity queries power the
dedup judge's top-K nearest lookup and the extractor's pre-extraction context.
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
from app.services.model_gate import model_gate

logger = logging.getLogger(__name__)


async def embed_claim_text(claim_text: str, db: Session) -> list[float] | None:
    """Compute the embedding for a claim's text. Returns None on any failure
    (logged at ERROR with the litellm body summary when available). Caller
    decides whether to retry / skip — upsert_claim_embedding additionally
    records the failure on the Claim row via embedding_failed_at."""
    embed_provider.reload_from_db(db)
    cfg = get_embed_config(db)
    try:
        params = await embed_provider.get_embedding_params(cfg.embed_model, claim_text)
        # See app/services/embeddings.py — embed family is compat with all in
        # the current policy, so this gate is a fast-path no-op today.
        with model_gate("embed", label="embed:claim"):
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
    """Embed `claim_id`'s text and store it on the claim row. Idempotent —
    overwrites whatever embedding was there before.

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
    claim.embedding = embedding
    claim.embedding_failed_at = None
    db.commit()
    return True


_CLAIM_REINDEX_BATCH_SIZE = 50


async def reindex_all_claims(db: Session, progress_cb=None) -> dict:
    """Regenerate embeddings for every claim with text. Returns
    {total, reindexed, failed}.

    Mirrors app.services.embeddings.reindex_all_docs's pagination shape.
    Needed after Settings → AI → Rebuild Index resizes claims.embedding to a
    new dimension: the resize itself clears every row (a pgvector column
    can only be widened/narrowed once empty), and unlike document_chunks —
    a purely derived index table the doc-level reindex already repopulates
    — Claim rows are real domain data that survive the resize with a NULL
    embedding, and need their own re-embed pass, not a fresh pipeline run.

    upsert_claim_embedding already commits per claim and is idempotent
    (records embedding_failed_at on failure, clears it on success), so a
    single claim's failure doesn't need special handling — it returns False
    rather than raising. The try/except here is defensive for a real DB
    error only, mirroring reindex_all_docs's rollback so one such error
    doesn't poison the session (or, via a stray uncommitted write, get
    flushed) for every claim processed after it in the same batch.
    """
    # claim_text is NOT NULL at the DB level; the != "" half excludes an
    # empty string, matching upsert_claim_embedding's own truthiness guard.
    base_query = db.query(Claim).filter(
        Claim.claim_text.isnot(None), Claim.claim_text != ""
    )
    total = base_query.count()
    reindexed = 0
    failed = 0

    offset = 0
    while offset < total:
        batch = (
            base_query.order_by(Claim.id)
            .limit(_CLAIM_REINDEX_BATCH_SIZE)
            .offset(offset)
            .all()
        )
        if not batch:
            break
        for claim in batch:
            try:
                ok = await upsert_claim_embedding(claim.id, db)
                if ok:
                    reindexed += 1
                else:
                    failed += 1
            except Exception as e:
                logger.warning(f"Claim reindex failed for claim {claim.id}: {e}")
                db.rollback()
                failed += 1
        offset += _CLAIM_REINDEX_BATCH_SIZE
        if progress_cb is not None:
            try:
                progress_cb(reindexed=reindexed, failed=failed)
            except Exception as cb_err:
                logger.debug(f"claim reindex progress_cb failed (continuing): {cb_err}")

    return {"total": total, "reindexed": reindexed, "failed": failed}


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

    # pgvector KNN, then filter in Python (or via a follow-up join). We
    # over-fetch a bit for case-scoping headroom.
    fetch_k = k * 4 if case_id else k
    distance = Claim.embedding.l2_distance(embedding)
    rows = (
        db.query(Claim.id, distance.label("distance"))
        .filter(Claim.embedding.isnot(None))
        .order_by(distance)
        .limit(fetch_k)
        .all()
    )

    if exclude_claim_id is not None:
        rows = [r for r in rows if r.id != exclude_claim_id]

    if not case_id:
        return [(r.id, r.distance) for r in rows[:k]]

    # Filter to claims with evidence in this case.
    candidate_ids = [r.id for r in rows]
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
    return [(r.id, r.distance) for r in rows if r.id in in_case_ids][:k]


def _expanding_int_list(name: str):
    """Helper: SQLAlchemy bindparam for an IN clause."""
    from sqlalchemy import bindparam

    return bindparam(name, expanding=True)
