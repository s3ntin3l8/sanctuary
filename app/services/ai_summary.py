import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.config import AI_BASE_URL, AI_SUMMARY_MODEL, AI_SYSTEM_PROMPT
from app.core.cache import cache, get_ai_summary_key
from app.models.database import Document, Proceeding
from app.services.ai_provider import ai_provider

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """You are a legal document analyst for Björn Hansen (client) and his lawyer Mr. Funk.
Extract metadata from the document and return a JSON object with these keys:
- az_court: The official court Aktenzeichen / docket number for the proceeding (e.g. 003 F 426/25; normalize spaces to dashes if needed).
- internal_id: The lawyer's internal reference number (e.g. 8124/25).
- sender: The organization or person who authored/sent the document.
- received_date: The date of the document or when it was received (YYYY-MM-DD).
- originator_type: Categorize as "court", "opposing", "own", "third_party", or "unknown".

Be concise. If information is not available, use null for that field.
Return ONLY valid JSON."""

SYSTEM_PROMPT = AI_SYSTEM_PROMPT if AI_SYSTEM_PROMPT else DEFAULT_SYSTEM_PROMPT


def _parse_summary_response(raw_text: str) -> dict:
    """Strip markdown fences and parse JSON from Ollama response text."""
    if not raw_text or not raw_text.strip():
        raise ValueError("AI returned an empty response")

    raw_text = raw_text.strip()

    # Handle markdown fences
    if "```" in raw_text:
        import re

        # Try to find a JSON block first
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if match:
            raw_text = match.group(1).strip()
        else:
            # Fallback: strip whatever is between the first and last triple backticks
            try:
                parts = raw_text.split("```")
                if len(parts) >= 3:
                    # Take the content between the first two fences
                    raw_text = parts[1].strip()
                    # If it starts with 'json', strip that too
                    if raw_text.lower().startswith("json"):
                        raw_text = raw_text[4:].strip()
            except Exception:
                pass

    # If it still doesn't look like JSON, try to find the first '{' and last '}'
    if not (raw_text.startswith("{") and raw_text.endswith("}")):
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start != -1 and end != -1:
            raw_text = raw_text[start : end + 1]
        elif start != -1:
            # Only start found? Maybe it's truncated?
            raw_text = raw_text[start:] + "}"
        else:
            # No braces found at all
            raise ValueError(
                f"AI response contains no JSON object: {raw_text[:100]}..."
            )

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        # One last attempt: maybe extra spaces or newlines?
        try:
            cleaned = raw_text.strip()
            return json.loads(cleaned)
        except Exception:
            # Log the full text for debugging in the log, but keep the exception message concise
            logger.debug(f"Malformed JSON from AI: {raw_text}")
            raise ValueError(
                f"Failed to parse AI response as JSON. Length: {len(raw_text)}. Preview: {raw_text[:100]}..."
            ) from e


def get_content_preview(doc: Document, max_chars: int = 4000) -> str:
    """Get a representative preview of document content using chunks if available."""
    if doc.meta and "chunks" in doc.meta and doc.meta["chunks"]:
        content = ""
        current_len = 0
        for chunk in doc.meta["chunks"]:
            text = chunk.get("text", "")
            if current_len + len(text) > max_chars:
                break
            content += text + "\n\n"
            current_len += len(text)
        if content:
            return content

    return (doc.content or "")[:max_chars]


async def generate_summary(doc: Document) -> dict:
    """Generate a 3-bullet management summary via configured AI provider using streaming."""
    content_preview = get_content_preview(doc, 4000)

    # Get provider-specific parameters
    params = await ai_provider.get_generate_params(
        model=AI_SUMMARY_MODEL,
        prompt=f"Document: {doc.title}\n\n{content_preview}",
        system_prompt=SYSTEM_PROMPT,
        stream=True,
        options={
            "num_ctx": 16384,
            "temperature": 0.4,
            "repeat_penalty": 1.2,
            "top_p": 0.9,
            "num_predict": 1000,
        },
    )
    ptype = await ai_provider.get_type()

    # Debug logging setup
    from app.config import DATA_DIR

    debug_dir = DATA_DIR / "ai_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_file = debug_dir / f"doc_{doc.id}_{int(datetime.now().timestamp())}.log"

    full_response = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        try:
            with open(debug_file, "a") as f:
                f.write(f"--- START REQUEST doc_id={doc.id} Provider={ptype} ---\n")
                f.write(f"Model: {AI_SUMMARY_MODEL}\n")
                f.write(f"Payload: {json.dumps(params['json'])}\n\n")

            async with client.stream(
                "POST", params["url"], json=params["json"], headers=params["headers"]
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue

                    chunk = ai_provider.parse_stream_line(line, ptype)
                    if not chunk:
                        continue

                    # Log only the actual tokens
                    token = chunk.get("thinking", "") + chunk.get("response", "")
                    if token:
                        with open(debug_file, "a") as f:
                            f.write(token)

                    if "response" in chunk:
                        full_response += chunk["response"]
                    if chunk.get("done"):
                        break

            with open(debug_file, "a") as f:
                f.write(f"\n--- END STREAM. Full Length: {len(full_response)} ---\n")
        except Exception as e:
            with open(debug_file, "a") as f:
                f.write(f"\n--- ERROR DURING STREAM: {str(e)} ---\n")
            raise

        if not full_response or not full_response.strip():
            raise ValueError(
                f"AI returned an empty response for '{doc.title}'. See {debug_file} for details."
            )

        logger.debug(f"AI raw response for '{doc.title}': {full_response}")
        return _parse_summary_response(full_response)


def enrich_document_with_ai(doc: Document, summary_data: dict, db: Session) -> None:
    """Refine document properties based on deep AI extraction."""
    from app.models.database import Case
    from app.models.enums import OriginatorType
    from app.services.ingestion.service import compute_review_reasons

    # 1. Update core text-based fields if missing or provided by AI
    if summary_data.get("sender") and not doc.sender:
        doc.sender = summary_data["sender"]

    if summary_data.get("originator_type"):
        try:
            val = summary_data["originator_type"].lower()
            if val in [e.value for e in OriginatorType]:
                doc.originator_type = OriginatorType(val)
        except Exception:
            pass

    # 2. Date parsing
    if summary_data.get("received_date") and not doc.received_date:
        try:
            parsed_date = datetime.strptime(summary_data["received_date"], "%Y-%m-%d")
            doc.received_date = parsed_date.replace(tzinfo=UTC)
        except Exception:
            pass

    # 3. Auto-Triage: match by Proceeding.az_court (per-court Aktenzeichen),
    # fallback to internal_id against Case.id.
    az_court = summary_data.get("az_court")
    internal_id = summary_data.get("internal_id")

    if doc.case_id == "_TRIAGE":
        matching_case = None
        matching_proceeding = None

        if az_court:
            matching_proceeding = (
                db.query(Proceeding).filter(Proceeding.az_court == az_court).first()
            )
            if matching_proceeding:
                matching_case = matching_proceeding.case

        if not matching_case and internal_id:
            matching_case = db.query(Case).filter(Case.id == internal_id).first()

        if matching_case:
            doc.case_id = matching_case.id
            if matching_proceeding:
                doc.proceeding_id = matching_proceeding.id
            logger.info(
                f"AI Auto-Triage: moved doc {doc.id} to case {matching_case.id}"
                + (
                    f" / proceeding {matching_proceeding.id}"
                    if matching_proceeding
                    else ""
                )
            )

    # 4. Re-evaluate review status
    reasons = compute_review_reasons(doc)
    doc.review_reasons = reasons
    doc.needs_review = len(reasons) > 0


async def summarize_document(doc_id: int, db: Session) -> Document:
    """Generate and persist an AI summary for a document."""
    cache_key = get_ai_summary_key(doc_id)

    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.content or doc.content.startswith("Conversion failed:"):
        return doc

    if doc.ai_summary and doc.ai_summary_status == "generated":
        cache.set(cache_key, doc, ttl=3600)
        return doc

    doc.ai_summary_status = "pending"
    db.commit()

    try:
        summary_data = await generate_summary(doc)

        # Phase 1: only apply metadata fields (az_court, sender, received_date, originator_type)
        # The 3-bullet ai_summary is now written by Phase 4 document_enricher
        enrich_document_with_ai(doc, summary_data, db)

        doc.ai_summary_status = "pending"  # Phase 4 enricher will set to "generated"
    except Exception as e:
        logger.error(f"Failed to generate summary for doc {doc_id}: {e}", exc_info=True)
        doc.ai_summary_status = "failed"
        doc.ai_summary = {"error": str(e)}

    db.commit()
    db.refresh(doc)

    if doc.ai_summary_status == "generated":
        cache.set(cache_key, doc, ttl=3600)

    return doc


def generate_summary_sync(doc: Document) -> dict:
    """Synchronous version of generate_summary using configured AI provider."""
    content_preview = get_content_preview(doc, 4000)

    # Get provider-specific parameters
    params = asyncio.run(
        ai_provider.get_generate_params(
            model=AI_SUMMARY_MODEL,
            prompt=f"Document: {doc.title}\n\n{content_preview}",
            system_prompt=SYSTEM_PROMPT,
            stream=True,
            options={
                "num_ctx": 16384,
                "temperature": 0.1,
            },
        )
    )
    ptype = asyncio.run(ai_provider.get_type())

    # Debug logging setup
    from app.config import DATA_DIR

    debug_dir = DATA_DIR / "ai_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_file = debug_dir / f"doc_{doc.id}_{int(datetime.now().timestamp())}_sync.log"

    full_response = ""
    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        try:
            with open(debug_file, "a") as f:
                f.write(
                    f"--- START REQUEST (SYNC) doc_id={doc.id} Provider={ptype} ---\n"
                )
                f.write(f"Model: {AI_SUMMARY_MODEL}\n")
                f.write(f"Payload: {json.dumps(params['json'])}\n\n")

            with client.stream(
                "POST", params["url"], json=params["json"], headers=params["headers"]
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue

                    chunk = ai_provider.parse_stream_line(line, ptype)
                    if not chunk:
                        continue

                    # Log only the actual tokens
                    token = chunk.get("thinking", "") + chunk.get("response", "")
                    if token:
                        with open(debug_file, "a") as f:
                            f.write(token)

                    if "response" in chunk:
                        full_response += chunk["response"]
                    if chunk.get("done"):
                        break

            with open(debug_file, "a") as f:
                f.write(f"\n--- END STREAM. Full Length: {len(full_response)} ---\n")
        except Exception as e:
            with open(debug_file, "a") as f:
                f.write(f"\n--- ERROR DURING STREAM: {str(e)} ---\n")
            raise

        if not full_response or not full_response.strip():
            raise ValueError(
                f"AI returned an empty response for '{doc.title}'. See {debug_file} for details."
            )

        logger.debug(f"AI raw response for '{doc.title}': {full_response}")
        return _parse_summary_response(full_response)


def _summarize_document_sync(doc_id: int, db: Session) -> Document:
    """Synchronous wrapper for fire-and-forget background summary generation."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.content or doc.content.startswith("Conversion failed:"):
        return doc

    doc.ai_summary_status = "pending"
    db.commit()

    try:
        summary_data = generate_summary_sync(doc)

        # Phase 1: only apply metadata fields (az_court, sender, received_date, originator_type)
        # The 3-bullet ai_summary is now written by Phase 4 document_enricher
        enrich_document_with_ai(doc, summary_data, db)

        doc.ai_summary_status = "pending"  # Phase 4 enricher will set to "generated"
    except Exception as e:
        logger.error(
            f"Failed to generate summary for doc {doc_id} (sync): {e}", exc_info=True
        )
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
            response = await client.get(f"{AI_BASE_URL}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m["name"] for m in data.get("models", [])]
            status["reachable"] = True

            # Check for model existence
            status["summary_model"] = any(AI_SUMMARY_MODEL in m for m in models)
    except Exception as e:
        status["error"] = str(e)

    return status
