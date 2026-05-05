import logging

import httpx

from app.config import SessionLocal
from app.models.database import Document
from app.services.ai_config import get_embed_config

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
        if doc.meta and "chunks" in doc.meta and doc.meta["chunks"]:
            current_len = 0
            for chunk in doc.meta["chunks"]:
                text = chunk.get("text", "")
                if current_len + len(text) > _EMBED_MAX_CHARS:
                    break
                content_snippet += text + "\n\n"
                current_len += len(text)

        if not content_snippet:
            content_snippet = doc.content[:_EMBED_MAX_CHARS]

        params = await embed_provider.get_embedding_params(
            cfg.embed_model, content_snippet
        )
        await embed_provider.get_type()

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


async def reindex_all_docs(db) -> dict:
    """Regenerate embeddings for all documents. Returns {total, reindexed, failed}."""
    from sqlalchemy import text

    # The user typically triggers reindex right after changing the embedding
    # model in settings — reload the provider config from DB so we use the
    # new model, not whatever was bound at app boot.
    embed_provider.reload_from_db(db)

    cfg = get_embed_config(db)
    docs = db.query(Document).filter(Document.content.isnot(None)).all()
    total = len(docs)
    reindexed = 0
    failed = 0

    for doc in docs:
        try:
            if not doc.content or doc.content.startswith("Conversion failed:"):
                continue
            content_snippet = ""
            if doc.meta and "chunks" in doc.meta and doc.meta["chunks"]:
                current_len = 0
                for chunk in doc.meta["chunks"]:
                    chunk_text = chunk.get("text", "")
                    if current_len + len(chunk_text) > _EMBED_MAX_CHARS:
                        break
                    content_snippet += chunk_text + "\n\n"
                    current_len += len(chunk_text)
            if not content_snippet:
                content_snippet = doc.content[:_EMBED_MAX_CHARS]
            params = await embed_provider.get_embedding_params(
                cfg.embed_model, content_snippet
            )
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    params["url"], json=params["json"], headers=params["headers"]
                )
                response.raise_for_status()
                data = response.json()
            embedding = data.get("embedding") or (
                data.get("data", [{}])[0].get("embedding") if data.get("data") else None
            )
            if embedding and len(embedding) == cfg.embed_dim:
                blob = _serialize(embedding)
                # See generate_embedding() above: vec0 doesn't honor INSERT OR
                # REPLACE; DELETE + INSERT keeps reindex idempotent.
                db.execute(
                    text("DELETE FROM document_vectors WHERE document_id = :doc_id"),
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
