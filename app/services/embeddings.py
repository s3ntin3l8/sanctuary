import json
import logging
import httpx
from sqlalchemy.orm import Session
from fastapi import BackgroundTasks
from app.models.database import Document
from app.config import SessionLocal, OLLAMA_BASE_URL, OLLAMA_EMBED_MODEL

logger = logging.getLogger(__name__)

async def generate_embedding(doc_id: int):
    """
    Background task to generate semantic embedding for a document via Ollama.
    Silently fails if Ollama is unavailable or model is missing.
    """
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc or not doc.content:
            return

        # Nomic has an 8192 context window, let's take a safe chunk
        content_snippet = doc.content[:24000]

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/embeddings",
                json={
                    "model": OLLAMA_EMBED_MODEL,
                    "prompt": content_snippet
                }
            )
            response.raise_for_status()
            data = response.json()
            embedding = data.get("embedding")
            
            if embedding:
                # sqlite-vec needs the array serialized as JSON or raw bytes depending on usage
                # We store it as a JSON string for now, and parse it in queries, or convert it to bytes.
                # Actually, `sqlite-vec` requires binary (f32). The `content_embedding` column in `Document` is `Text`.
                # We can store it as JSON string and handle it during search.
                doc.content_embedding = json.dumps(embedding)
                db.commit()

    except Exception as e:
        logger.warning(f"Failed to generate embedding for doc {doc_id}: {e}")
    finally:
        db.close()

def trigger_embedding_background(doc_id: int, background_tasks: BackgroundTasks):
    background_tasks.add_task(generate_embedding, doc_id)
