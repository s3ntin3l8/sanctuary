import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.config import AI_BASE_URL
from app.core.async_utils import run_async
from app.core.cache import cache, get_ai_summary_key
from app.models.database import Document, Proceeding
from app.services.ai_config import get_effective_config
from app.services.ai_provider import ai_provider
from app.services.intelligence._json import parse_json_response
from app.services.intelligence.prompts import PHASE1_METADATA_SYSTEM

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = PHASE1_METADATA_SYSTEM

_TAIL_CHARS = 2000


def get_content_preview(
    doc: Document, max_chars: int = 4000, include_tail: bool = True
) -> str:
    """Get a representative preview of document content using chunks if available.

    When include_tail=True and content exceeds max_chars, returns a head+tail window.
    Output length may exceed max_chars by the length of the truncation marker."""
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

    content = doc.content or ""

    # If content fits within max_chars, return as-is
    if len(content) <= max_chars:
        return content

    # For long content with include_tail, use head+tail window
    if include_tail and max_chars > _TAIL_CHARS:
        head_chars = max_chars - _TAIL_CHARS
        return (
            content[:head_chars]
            + "\n\n[... truncated middle ...]\n\n"
            + content[-_TAIL_CHARS:]
        )

    # Fallback: return head-only (include_tail=False or max_chars <= _TAIL_CHARS)
    return content[:max_chars]


async def generate_summary(doc: Document, db=None) -> dict:
    """Generate a 3-bullet management summary via configured AI provider using streaming."""
    cfg = get_effective_config(db)
    if db is not None:
        ai_provider.reload_from_db(db)
    content_preview = get_content_preview(doc, 4000)

    # Heuristic hints for verification
    hints = {
        "az_court": doc.proceeding.az_court if doc.proceeding else None,
        "sender": doc.sender,
        "received_date": doc.received_date.strftime("%Y-%m-%d")
        if doc.received_date
        else None,
        "originator_type": doc.originator_type.value if doc.originator_type else None,
    }
    hints_str = json.dumps(hints, indent=2)

    prompt = (
        f"Document: {doc.title}\n\n"
        f"### Heuristic Hints (found by regex, please verify):\n{hints_str}\n\n"
        f"### Document Content Preview:\n{content_preview}"
    )

    # Get provider-specific parameters
    params = await ai_provider.get_generate_params(
        model=cfg.summary_model,
        prompt=prompt,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        stream=True,
        options={
            "num_ctx": 16384,
            "temperature": 0.4,
            "repeat_penalty": 1.2,
            "top_p": 0.9,
            "num_predict": 1000,
            "max_tokens": 1000,
        },
    )
    ptype = await ai_provider.get_type()

    # Debug logging setup
    from app.config import DATA_DIR

    debug_dir = DATA_DIR / "ai_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_file = debug_dir / f"doc_{doc.id}_{int(datetime.now().timestamp())}.log"

    full_thinking = ""
    full_response = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        try:
            with open(debug_file, "a") as f:
                f.write(f"--- START REQUEST doc_id={doc.id} Provider={ptype} ---\n")
                f.write(f"Model: {cfg.summary_model}\n")
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

                    # Log tokens to debug file
                    token = chunk.get("thinking", "") + chunk.get("response", "")
                    if token:
                        with open(debug_file, "a") as f:
                            f.write(token)

                    if "thinking" in chunk:
                        full_thinking += chunk["thinking"]
                    if "response" in chunk:
                        full_response += chunk["response"]

                    if chunk.get("done"):
                        break

            with open(debug_file, "a") as f:
                f.write(
                    f"\n--- END STREAM. Full Length: {len(full_response)} Thinking Length: {len(full_thinking)} ---\n"
                )
        except Exception as e:
            with open(debug_file, "a") as f:
                f.write(f"\n--- ERROR DURING STREAM: {str(e)} ---\n")
            raise

    if not full_response or not full_response.strip():
        # If we have thinking but no response, the model might be stuck or refusing
        refusal_msg = ""
        if full_thinking:
            refusal_msg = f" (Thinking was present: {full_thinking[:100]}...)"
        raise ValueError(
            f"AI returned an empty response for '{doc.title}'.{refusal_msg} See {debug_file} for details."
        )

    logger.debug(f"AI raw response for '{doc.title}': {full_response}")
    return parse_json_response(full_response)


def enrich_document_with_ai(doc: Document, summary_data: dict, db: Session) -> None:
    """Refine document properties based on deep AI extraction."""
    from app.models.database import Case
    from app.models.enums import OriginatorType
    from app.services.ingestion.service import compute_review_reasons

    # 1. Update core fields (AI overrides heuristics)
    if summary_data.get("sender"):
        doc.sender = summary_data["sender"]

    if summary_data.get("originator_type"):
        try:
            val = summary_data["originator_type"].lower()
            if val in [e.value for e in OriginatorType]:
                doc.originator_type = OriginatorType(val)
        except Exception:
            pass

    if summary_data.get("received_date"):
        try:
            parsed_date = datetime.strptime(summary_data["received_date"], "%Y-%m-%d")
            doc.received_date = parsed_date.replace(tzinfo=UTC)
        except Exception:
            pass

    if summary_data.get("internal_id"):
        doc.internal_id = summary_data["internal_id"]

    # 2. Update confidence scores
    ai_conf = summary_data.get("confidence")
    if ai_conf and isinstance(ai_conf, dict):
        # Create a new dict to ensure SQLAlchemy detects the change
        new_conf = dict(doc.extraction_confidence or {})
        for key, val in ai_conf.items():
            if val in ("high", "medium", "low"):
                new_conf[key] = val
        doc.extraction_confidence = new_conf

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

    if doc.ai_summary:
        cache.set(cache_key, doc, ttl=3600)
        return doc

    db.commit()

    try:
        summary_data = await generate_summary(doc, db=db)

        # Phase 1: only apply metadata fields (az_court, sender, received_date, originator_type)
        # The 3-bullet ai_summary is now written by Phase 4 document_enricher
        enrich_document_with_ai(doc, summary_data, db)
    except Exception as e:
        logger.error(f"Failed to generate summary for doc {doc_id}: {e}", exc_info=True)
        doc.ai_summary = {"error": str(e)}

    db.commit()
    db.refresh(doc)

    if doc.ai_summary:
        cache.set(cache_key, doc, ttl=3600)

    return doc


def generate_summary_sync(doc: Document, db=None) -> dict:
    """Synchronous version of generate_summary using configured AI provider."""
    cfg = get_effective_config(db)
    if db is not None:
        ai_provider.reload_from_db(db)
    content_preview = get_content_preview(doc, 4000)

    # Heuristic hints for verification
    hints = {
        "az_court": doc.proceeding.az_court if doc.proceeding else None,
        "sender": doc.sender,
        "received_date": doc.received_date.strftime("%Y-%m-%d")
        if doc.received_date
        else None,
        "originator_type": doc.originator_type.value if doc.originator_type else None,
    }
    hints_str = json.dumps(hints, indent=2)

    prompt = (
        f"Document: {doc.title}\n\n"
        f"### Heuristic Hints (found by regex, please verify):\n{hints_str}\n\n"
        f"### Document Content Preview:\n{content_preview}"
    )

    # Get provider-specific parameters
    params = run_async(
        ai_provider.get_generate_params(
            model=cfg.summary_model,
            prompt=prompt,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            stream=True,
            options={
                "num_ctx": 16384,
                "temperature": 0.1,
                "num_predict": 1000,
                "max_tokens": 1000,
            },
        )
    )
    ptype = run_async(ai_provider.get_type())

    # Debug logging setup
    from app.config import DATA_DIR

    debug_dir = DATA_DIR / "ai_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_file = debug_dir / f"doc_{doc.id}_{int(datetime.now().timestamp())}_sync.log"

    full_thinking = ""
    full_response = ""
    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        try:
            with open(debug_file, "a") as f:
                f.write(
                    f"--- START REQUEST (SYNC) doc_id={doc.id} Provider={ptype} ---\n"
                )
                f.write(f"Model: {cfg.summary_model}\n")
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

                    # Log tokens to debug file
                    token = chunk.get("thinking", "") + chunk.get("response", "")
                    if token:
                        with open(debug_file, "a") as f:
                            f.write(token)

                    if "thinking" in chunk:
                        full_thinking += chunk["thinking"]
                    if "response" in chunk:
                        full_response += chunk["response"]
                    if chunk.get("done"):
                        break

            with open(debug_file, "a") as f:
                f.write(
                    f"\n--- END STREAM. Full Length: {len(full_response)} Thinking Length: {len(full_thinking)} ---\n"
                )
        except Exception as e:
            with open(debug_file, "a") as f:
                f.write(f"\n--- ERROR DURING STREAM: {str(e)} ---\n")
            raise

    if not full_response or not full_response.strip():
        refusal_msg = ""
        if full_thinking:
            refusal_msg = f" (Thinking was present: {full_thinking[:100]}...)"
        raise ValueError(
            f"AI returned an empty response for '{doc.title}'.{refusal_msg} See {debug_file} for details."
        )

    logger.debug(f"AI raw response for '{doc.title}': {full_response}")
    return parse_json_response(full_response)


def _summarize_document_sync(doc_id: int, db: Session) -> Document:
    """Synchronous wrapper for fire-and-forget background summary generation."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc or not doc.content or doc.content.startswith("Conversion failed:"):
        return doc

    db.commit()

    try:
        summary_data = generate_summary_sync(doc, db=db)

        # Phase 1: only apply metadata fields (az_court, sender, received_date, originator_type)
        # The 3-bullet ai_summary is now written by Phase 4 document_enricher
        enrich_document_with_ai(doc, summary_data, db)
    except Exception as e:
        logger.error(
            f"Failed to generate summary for doc {doc_id} (sync): {e}", exc_info=True
        )
        doc.ai_summary = {"error": str(e)}
        db.commit()
        raise e

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
            cfg = get_effective_config(None)
            status["summary_model"] = any(cfg.summary_model in m for m in models)
    except Exception as e:
        status["error"] = str(e)

    return status
