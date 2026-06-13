import logging

import httpx

from app.config import SessionLocal
from app.models.database import Document
from app.services.ai_config import get_embed_config
from app.services.ai_inflight import track_ai_call_async
from app.services.model_gate import model_gate

logger = logging.getLogger(__name__)

# nomic-embed-text:v1.5 has an 8192-token context window; ~3 chars/token for German legal text
_EMBED_MAX_CHARS = 22000

from app.services.ai_provider import embed_provider


def _serialize(vec: list[float]) -> bytes:
    """Convert a float list to sqlite-vec f32 blob."""
    from sqlite_vec import serialize_float32

    return serialize_float32(vec)


async def generate_embedding(doc_id: int):
    """Generate and store the document embedding.

    Raises on any failure (network error, JSON parse, dim mismatch, no vector) —
    the caller (`generate_embedding_task`) catches and marks the stage failed.
    Silent failure here was previously masking docs that never got an embedding
    written but were still flagged COMPLETED, so search would miss them.
    """
    db = SessionLocal()
    try:
        embed_provider.reload_from_db(db)
        cfg = get_embed_config(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc or not doc.content or doc.content.startswith("Conversion failed:"):
            return

        content_snippet = ""
        chunks = doc.meta.get("chunks", []) if doc.meta else []
        if chunks:
            current_len = 0
            for chunk in chunks:
                text = chunk.get("text", "")
                if current_len + len(text) > _EMBED_MAX_CHARS:
                    break
                content_snippet += text + "\n\n"
                current_len += len(text)
            if not content_snippet:
                content_snippet = chunks[0].get("text", "")[:_EMBED_MAX_CHARS]

        if not content_snippet:
            content_snippet = doc.content[:_EMBED_MAX_CHARS]

        params = await embed_provider.get_embedding_params(
            cfg.embed_model, content_snippet
        )
        await embed_provider.get_type()

        # Acquire the embed-family lock. The current policy treats embed as
        # compatible with chandra and qwen (small model fits alongside), so
        # this is a fast-path no-op — but the call site stays uniform if the
        # user later swaps in a larger embedding model that needs exclusion.
        with model_gate("embed", label=f"embed:doc:{doc_id}"):
            async with track_ai_call_async(f"embed:doc:{doc_id}"):
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        params["url"], json=params["json"], headers=params["headers"]
                    )
                    response.raise_for_status()
                    data = response.json()

        embedding = None
        if "embedding" in data:
            embedding = data["embedding"]
        elif (
            "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0
        ):
            embedding = data["data"][0].get("embedding")

        if not embedding:
            raise ValueError(f"Embedding provider returned no vector for doc {doc_id}")
        if len(embedding) != cfg.embed_dim:
            raise ValueError(
                f"Embedding dim mismatch for doc {doc_id}: provider returned "
                f"{len(embedding)}, config embed_dim={cfg.embed_dim}. "
                "Check AI_EMBED_DIM matches the embedding model."
            )

        from sqlalchemy import text

        blob = _serialize(embedding)
        # vec0 virtual tables don't honor INSERT OR REPLACE on conflict, so
        # retries hit a UNIQUE-on-primary-key error. Explicit DELETE + INSERT
        # makes the write idempotent for re-ingestion / pipeline retries.
        db.execute(
            text("DELETE FROM document_vectors WHERE document_id = :doc_id"),
            {"doc_id": doc_id},
        )
        db.execute(
            text(
                "INSERT INTO document_vectors(document_id, embedding) VALUES (:doc_id, :embedding)"
            ),
            {"doc_id": doc_id, "embedding": blob},
        )
        db.commit()
    finally:
        db.close()


def nearest_document_ids(query_text: str, db, *, k: int) -> list[int]:
    """Return up to `k` document_ids whose stored embedding is nearest to
    `query_text`, closest first.

    Synchronous, best-effort: returns ``[]`` on any failure (provider down,
    dim mismatch, sqlite-vec unavailable) so callers can fall back gracefully.
    Mirrors the embed+vec0 MATCH pattern in
    ``SearchService._semantic_document_ids``; lives here so the document-vector
    KNN has one home.
    """
    from sqlalchemy import text

    from app.core.async_utils import run_async

    if not query_text:
        return []
    try:
        embed_provider.reload_from_db(db)
        cfg = get_embed_config(db)
        params = run_async(
            embed_provider.get_embedding_params(cfg.embed_model, query_text)
        )
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                params["url"], json=params["json"], headers=params["headers"]
            )
            resp.raise_for_status()
            data = resp.json()
        embedding = data.get("embedding") or (
            data.get("data", [{}])[0].get("embedding") if data.get("data") else None
        )
        if not embedding or len(embedding) != cfg.embed_dim:
            return []
        blob = _serialize(embedding)
        rows = db.execute(
            text(
                "SELECT document_id, distance FROM document_vectors "
                "WHERE embedding MATCH :blob ORDER BY distance LIMIT :k"
            ),
            {"blob": blob, "k": k},
        ).fetchall()
        return [row[0] for row in rows]
    except Exception as e:  # noqa: BLE001 — best-effort; never block the caller
        logger.debug("nearest_document_ids unavailable: %s", e)
        return []


_REINDEX_BATCH_SIZE = 50


async def reindex_all_docs(db, progress_cb=None) -> dict:
    """Regenerate embeddings for all documents. Returns {total, reindexed, failed}.

    Paginated in batches of _REINDEX_BATCH_SIZE so a corpus of N thousand
    documents doesn't all sit in Python memory at once. Each doc still
    commits independently (vec0 requires DELETE+INSERT for idempotency).
    Progress is logged at INFO every batch so the user can tail the log.

    progress_cb(reindexed: int, failed: int) is called at each batch
    boundary; the Celery wrapper uses this to update UserSettings so the
    HTMX polling UI advances. Best-effort: callback exceptions are
    swallowed so an SQLite write contention doesn't kill the reindex.
    """
    from sqlalchemy import text

    # The user typically triggers reindex right after changing the embedding
    # model in settings — reload the provider config from DB so we use the
    # new model, not whatever was bound at app boot.
    embed_provider.reload_from_db(db)

    cfg = get_embed_config(db)

    base_query = db.query(Document).filter(Document.content.isnot(None))
    total = base_query.count()
    reindexed = 0
    failed = 0

    offset = 0
    while offset < total:
        batch = (
            base_query.order_by(Document.id)
            .limit(_REINDEX_BATCH_SIZE)
            .offset(offset)
            .all()
        )
        if not batch:
            break
        for doc in batch:
            try:
                if not doc.content or doc.content.startswith("Conversion failed:"):
                    continue
                content_snippet = ""
                chunks = doc.meta.get("chunks", []) if doc.meta else []
                if chunks:
                    current_len = 0
                    for chunk in chunks:
                        chunk_text = chunk.get("text", "")
                        if current_len + len(chunk_text) > _EMBED_MAX_CHARS:
                            break
                        content_snippet += chunk_text + "\n\n"
                        current_len += len(chunk_text)
                    if not content_snippet:
                        content_snippet = chunks[0].get("text", "")[:_EMBED_MAX_CHARS]
                if not content_snippet:
                    content_snippet = doc.content[:_EMBED_MAX_CHARS]
                params = await embed_provider.get_embedding_params(
                    cfg.embed_model, content_snippet
                )
                with model_gate("embed", label=f"embed:doc:{doc.id}"):
                    async with track_ai_call_async(f"embed:doc:{doc.id}"):
                        async with httpx.AsyncClient(timeout=60.0) as client:
                            response = await client.post(
                                params["url"],
                                json=params["json"],
                                headers=params["headers"],
                            )
                            response.raise_for_status()
                            data = response.json()
                embedding = data.get("embedding") or (
                    data.get("data", [{}])[0].get("embedding")
                    if data.get("data")
                    else None
                )
                if embedding and len(embedding) == cfg.embed_dim:
                    blob = _serialize(embedding)
                    # See generate_embedding() above: vec0 doesn't honor INSERT OR
                    # REPLACE; DELETE + INSERT keeps reindex idempotent.
                    db.execute(
                        text(
                            "DELETE FROM document_vectors WHERE document_id = :doc_id"
                        ),
                        {"doc_id": doc.id},
                    )
                    db.execute(
                        text(
                            "INSERT INTO document_vectors(document_id, embedding) VALUES (:doc_id, :embedding)"
                        ),
                        {"doc_id": doc.id, "embedding": blob},
                    )
                    db.commit()
                    reindexed += 1
                else:
                    failed += 1
            except Exception as e:
                logger.warning(f"Reindex failed for doc {doc.id}: {e}")
                failed += 1
        offset += _REINDEX_BATCH_SIZE
        logger.info(
            f"reindex_all_docs: {min(offset, total)}/{total} processed "
            f"({reindexed} ok, {failed} failed)"
        )
        if progress_cb is not None:
            try:
                progress_cb(reindexed=reindexed, failed=failed)
            except Exception as cb_err:
                logger.debug(f"reindex progress_cb failed (continuing): {cb_err}")

    return {"total": total, "reindexed": reindexed, "failed": failed}


_VEC0_DIM_RE = __import__("re").compile(
    r"embedding\s+float\s*\[\s*(\d+)\s*\]", __import__("re").IGNORECASE
)


def verify_vec0_dim(db, expected_dim: int) -> tuple[bool, int | None]:
    """Read the document_vectors vec0 schema and compare its declared dimension.

    Returns (matches, actual_dim). actual_dim is None if the schema can't be parsed.
    Used by the lifespan startup hook to fail loudly if AI_EMBED_DIM was changed
    without recreating the vec0 table — vec0 can't be ALTERed, so a mismatch
    silently breaks every embedding write at the per-write dim guard.
    """
    from sqlalchemy import text

    row = db.execute(
        text("SELECT sql FROM sqlite_master WHERE name = 'document_vectors'")
    ).fetchone()
    if not row or not row[0]:
        return False, None
    match = _VEC0_DIM_RE.search(row[0])
    if not match:
        return False, None
    actual = int(match.group(1))
    return actual == expected_dim, actual
