import logging

import httpx
from fastapi import BackgroundTasks

from app.config import SessionLocal
from app.models.database import Document
from app.services.ai_config import get_effective_config

logger = logging.getLogger(__name__)

# nomic-embed-text:v1.5 has an 8192-token context window; ~3 chars/token for German legal text
_EMBED_MAX_CHARS = 22000

from app.services.ai_provider import ai_provider


def _serialize(vec: list[float]) -> bytes:
    """Convert a float list to sqlite-vec f32 blob."""
    from sqlite_vec import serialize_float32

    return serialize_float32(vec)


async def generate_embedding(doc_id: int):
    """Background task: generate embedding and store in document_vectors vec0 table."""
    db = SessionLocal()
    try:
        cfg = get_effective_config(db)
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

        params = await ai_provider.get_embedding_params(
            cfg.embed_model, content_snippet
        )
        await ai_provider.get_type()

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
                "data" in data
                and isinstance(data["data"], list)
                and len(data["data"]) > 0
            ):
                embedding = data["data"][0].get("embedding")

            if embedding and len(embedding) == cfg.embed_dim:
                from sqlalchemy import text

                blob = _serialize(embedding)
                db.execute(
                    text(
                        "INSERT OR REPLACE INTO document_vectors(document_id, embedding) VALUES (:doc_id, :embedding)"
                    ),
                    {"doc_id": doc_id, "embedding": blob},
                )
                db.commit()

    except Exception as e:
        logger.warning(f"Failed to generate embedding for doc {doc_id}: {e}")
    finally:
        db.close()


def trigger_embedding_background(doc_id: int, background_tasks: BackgroundTasks):
    background_tasks.add_task(generate_embedding, doc_id)


async def reindex_all_docs(db) -> dict:
    """Regenerate embeddings for all documents. Returns {total, reindexed, failed}."""
    from sqlalchemy import text

    cfg = get_effective_config(db)
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
            params = await ai_provider.get_embedding_params(
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
                db.execute(
                    text(
                        "INSERT OR REPLACE INTO document_vectors(document_id, embedding) VALUES (:doc_id, :embedding)"
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
