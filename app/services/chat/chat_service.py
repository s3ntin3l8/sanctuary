"""Streaming chat service for document and case scopes.

Yields SSE-formatted lines:
  data: {"type": "token", "t": "..."}
  data: {"type": "citations", "docs": [...]}
  data: {"type": "done"}
"""

import json
import logging
import re
from collections.abc import AsyncIterator

import httpx
from sqlalchemy.orm import Session

from app.models.database import Case, Conversation, Document
from app.repositories.chat import ChatRepository
from app.services.ai_config import get_chat_config
from app.services.ai_inflight import track_ai_call_async
from app.services.ai_provider import chat_provider
from app.services.chat.context_builder import (
    build_case_chat_prompt,
    build_document_chat_prompt,
)
from app.services.chat.prompts import CASE_CHAT_SYSTEM, DOC_CHAT_SYSTEM
from app.services.chat.retrieval import retrieve_top_docs

logger = logging.getLogger(__name__)

_DOC_REF_RE = re.compile(r"\[DOC:(\d+)(?:#p=(\d+))?\]")


async def stream_answer(
    conversation: Conversation,
    user_message: str,
    db: Session,
    proceeding_id: int | None = None,
) -> AsyncIterator[str]:
    """Persist user message, stream the assistant reply, persist + emit citations."""
    chat_provider.reload_from_db(db)
    cfg = get_chat_config(db)
    repo = ChatRepository(db)

    repo.add_message(conversation.id, "user", user_message)
    history = repo.messages(conversation.id)[:-1]  # exclude the message just added

    scope_type = conversation.scope_type
    scope_id = conversation.scope_id

    if scope_type == "document":
        doc = db.get(Document, int(scope_id))
        if not doc:
            yield _sse({"type": "token", "t": f"Document {scope_id} not found."})
            yield _sse({"type": "done"})
            return
        prompt = build_document_chat_prompt(doc, db, history, user_message)
        system_prompt = DOC_CHAT_SYSTEM

    elif scope_type == "case":
        case = db.get(Case, scope_id)
        if not case:
            yield _sse({"type": "token", "t": f"Case {scope_id} not found."})
            yield _sse({"type": "done"})
            return
        hits = await retrieve_top_docs(
            user_message, scope_id, db, proceeding_id=proceeding_id
        )
        prompt = build_case_chat_prompt(case, db, history, user_message, hits)
        system_prompt = CASE_CHAT_SYSTEM

    else:
        yield _sse({"type": "token", "t": "Unknown chat scope."})
        yield _sse({"type": "done"})
        return

    params = await chat_provider.get_generate_params(
        model=cfg.summary_model,
        prompt=prompt,
        system_prompt=system_prompt,
        stream=True,
        options={"num_ctx": 32768, "temperature": 0.2, "num_predict": 800},
    )
    ptype = await chat_provider.get_type()

    full_response = ""
    async with track_ai_call_async(f"chat:{scope_type}:{scope_id}"):
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=90.0)) as client:
            try:
                async with client.stream(
                    "POST",
                    params["url"],
                    json=params["json"],
                    headers=params["headers"],
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        chunk = chat_provider.parse_stream_line(line, ptype)
                        if not chunk:
                            continue
                        token = chunk.get("response", "")
                        if token:
                            full_response += token
                            yield _sse({"type": "token", "t": token})
                        if chunk.get("done"):
                            break
            except Exception as e:
                err_str = str(e).lower()
                if "context" in err_str and any(
                    w in err_str for w in ("size", "window", "exceeded", "length")
                ):
                    logger.warning("Chat context too large for model: %s", e)
                    yield _sse(
                        {
                            "type": "token",
                            "t": "\n\nThe conversation is too long for the model's context window. "
                            "Start a new chat, or ask about a specific document rather than the full case.",
                        }
                    )
                else:
                    logger.error("Chat stream error: %s", e)
                    yield _sse({"type": "token", "t": f"\n\n[Stream error: {e}]"})

    cited_ids, cited_refs = _extract_citations(full_response)

    citation_docs = []
    if cited_ids:
        docs_cited = db.query(Document).filter(Document.id.in_(cited_ids)).all()
        doc_map = {d.id: d for d in docs_cited}
        for ref in cited_refs:
            doc_id = ref["doc_id"]
            d = doc_map.get(doc_id)
            if d:
                citation_docs.append(
                    {
                        "doc_id": d.id,
                        "case_id": d.case_id,
                        "title": d.title or "Untitled",
                        "passage_idx": ref["passage_idx"],
                    }
                )

    if citation_docs:
        yield _sse({"type": "citations", "docs": citation_docs})

    repo.add_message(
        conversation.id,
        "assistant",
        full_response,
        context_document_ids=list(cited_ids) if cited_ids else None,
    )

    yield _sse({"type": "done"})


def _extract_citations(text: str) -> tuple[set[int], list[dict]]:
    doc_ids = set()
    unique_refs = {}
    for m in _DOC_REF_RE.finditer(text):
        doc_id = int(m.group(1))
        passage_idx = m.group(2)
        doc_ids.add(doc_id)
        ref_key = f"{doc_id}#p={passage_idx}" if passage_idx else str(doc_id)
        if ref_key not in unique_refs:
            unique_refs[ref_key] = {"doc_id": doc_id, "passage_idx": passage_idx}
    return doc_ids, list(unique_refs.values())


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
