import json
import logging

import httpx
from fastapi import BackgroundTasks

from app.config import AI_BASE_URL, AI_EMBED_MODEL, SessionLocal
from app.models.database import Document

logger = logging.getLogger(__name__)


from app.services.ai_provider import ai_provider


async def generate_embedding(doc_id: int):
    """
    Background task to generate semantic embedding for a document via configured AI provider.
    Silently fails if provider is unavailable or model is missing.
    """
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc or not doc.content or doc.content.startswith("Conversion failed:"):
            return

        # Use hierarchical chunks if available for a better representative snippet
        content_snippet = ""
        if doc.meta and "chunks" in doc.meta and doc.meta["chunks"]:
            # Combine the first few chunks until we hit a reasonable limit
            current_len = 0
            for chunk in doc.meta["chunks"]:
                text = chunk.get("text", "")
                if current_len + len(text) > 16000:
                    break
                content_snippet += text + "\n\n"
                current_len += len(text)

        if not content_snippet:
            # Fallback to character-based slicing
            # Nomic has an 8192 context window, let's take a safe chunk
            # 16,000 chars is roughly 4,000-5,000 tokens, well within the 8,192 limit.
            content_snippet = doc.content[:16000]

        params = await ai_provider.get_embedding_params(AI_EMBED_MODEL, content_snippet)
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
                # OpenAI format: {"data": [{"embedding": [...], ...}]}
                embedding = data["data"][0].get("embedding")

            if embedding:
                doc.content_embedding = json.dumps(embedding)
                db.commit()

    except Exception as e:
        logger.warning(f"Failed to generate embedding for doc {doc_id}: {e}")
    finally:
        db.close()


def trigger_embedding_background(doc_id: int, background_tasks: BackgroundTasks):
    background_tasks.add_task(generate_embedding, doc_id)


async def check_embedding_status() -> dict:
    """Check if embedding model is pulled."""
    import httpx

    from app.config import AI_EMBED_MODEL

    status = {"reachable": False, "embedding_model": False, "error": None}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{AI_BASE_URL}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m["name"] for m in data.get("models", [])]
            status["reachable"] = True

            # Check for model existence
            status["embedding_model"] = any(AI_EMBED_MODEL in m for m in models)
    except Exception as e:
        status["error"] = str(e)

    return status
