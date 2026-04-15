import asyncio
import json
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.config import OLLAMA_BASE_URL, OLLAMA_SUMMARY_MODEL
from app.models.database import Document

SYSTEM_PROMPT = """You are a legal document analyst.
Analyze the provided document and return a JSON object with exactly these three keys:
- legal_significance: What does this document mean for our legal position?
  (1-2 sentences)
- required_action: What needs to be done and by when?
  (1-2 sentences, or "No immediate action required")
- financial_impact: Any fees, costs, or financial implications?
  (1-2 sentences, or "No direct financial impact")

Be concise and specific. If information is not available in the document,
say so explicitly.
Return ONLY valid JSON, no markdown formatting."""


def _parse_summary_response(raw_text: str) -> dict:
    """Strip markdown fences and parse JSON from Ollama response text."""
    raw_text = raw_text.strip()

    # Handle markdown fences
    if raw_text.startswith("```"):
        # Look for the first block of code
        import re

        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if match:
            raw_text = match.group(1)
        else:
            # Fallback for simple fence stripping
            raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # If it still doesn't look like JSON, try to find the first '{' and last '}'
    if not (raw_text.startswith("{") and raw_text.endswith("}")):
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start != -1 and end != -1:
            raw_text = raw_text[start : end + 1]

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        # One last attempt: maybe extra spaces or newlines?
        try:
            return json.loads(raw_text.strip())
        except Exception:
            raise ValueError(
                f"Failed to parse AI response as JSON: {raw_text[:200]}..."
            ) from e


async def generate_summary(doc_content: str, doc_title: str = "") -> dict:
    """Generate a 3-bullet management summary via Ollama."""
    content_preview = doc_content[:4000]
    prompt = f"Document: {doc_title}\n\n{content_preview}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": OLLAMA_SUMMARY_MODEL,
                "prompt": SYSTEM_PROMPT + "\n\n" + prompt,
                "stream": False,
                "format": "json",
            },
        )
        response.raise_for_status()
        result = response.json()
        raw_text = result.get("response", "")

        return _parse_summary_response(raw_text)


async def summarize_document(doc_id: int, db: Session) -> Document:
    """Generate and persist an AI summary for a document."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.content:
        return doc

    doc.ai_summary_status = "pending"
    db.commit()

    try:
        summary = await generate_summary(doc.content, doc.title or "")
        doc.ai_summary = summary
        doc.ai_summary_created_at = datetime.now(UTC)
        doc.ai_summary_status = "generated"
    except Exception as e:
        doc.ai_summary_status = "failed"
        doc.ai_summary = {"error": str(e)}

    db.commit()
    db.refresh(doc)
    return doc


def _summarize_document_sync(doc_id: int, db: Session) -> Document:
    """Synchronous wrapper for fire-and-forget background summary generation."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.content:
        return doc

    doc.ai_summary_status = "pending"
    db.commit()

    try:
        content_preview = doc.content[:4000]
        prompt = f"Document: {doc.title or ''}\n\n{content_preview}"

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": OLLAMA_SUMMARY_MODEL,
                    "prompt": SYSTEM_PROMPT + "\n\n" + prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            response.raise_for_status()
            result = response.json()
            raw_text = result.get("response", "")

            summary = _parse_summary_response(raw_text)
            doc.ai_summary = summary
            doc.ai_summary_created_at = datetime.now(UTC)
            doc.ai_summary_status = "generated"
    except Exception as e:
        doc.ai_summary_status = "failed"
        doc.ai_summary = {"error": str(e)}

    db.commit()
    db.refresh(doc)
    return doc


def trigger_summary_async(doc_id: int):
    """Fire-and-forget summary generation for post-ingestion use."""

    def _run():
        from app.config import SessionLocal

        db = SessionLocal()
        try:
            _summarize_document_sync(doc_id, db)
        finally:
            db.close()

    asyncio.create_task(asyncio.to_thread(_run))


def trigger_summary_background(doc_id: int, background_tasks) -> None:
    """Background task-based summary generation. Safer than fire-and-forget."""
    from app.config import SessionLocal

    def _run():
        db = SessionLocal()
        try:
            _summarize_document_sync(doc_id, db)
        finally:
            db.close()

    background_tasks.add_task(_run)


async def check_ollama_status() -> dict:
    """Check if Ollama is reachable and models are pulled."""
    status = {"reachable": False, "summary_model": False, "error": None}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m["name"] for m in data.get("models", [])]
            status["reachable"] = True

            # Check for model existence
            status["summary_model"] = any(OLLAMA_SUMMARY_MODEL in m for m in models)
    except Exception as e:
        status["error"] = str(e)

    return status
