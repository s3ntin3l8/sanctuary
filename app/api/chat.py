"""Chat API — document-scoped and case-scoped AI conversations."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.repositories.chat import ChatRepository
from app.services.chat.chat_service import stream_answer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ConversationRequest(BaseModel):
    scope_type: str  # "document" | "case"
    scope_id: str
    force_new: bool = False


class MessageRequest(BaseModel):
    content: str


@router.post("/conversations")
def get_or_create_conversation(req: ConversationRequest, db: Session = Depends(get_db)):
    """Return the active conversation for a scope, creating one if needed."""
    if req.scope_type not in ("document", "case"):
        raise HTTPException(
            status_code=400, detail="scope_type must be 'document' or 'case'"
        )
    repo = ChatRepository(db)
    conv = repo.get_or_create(req.scope_type, req.scope_id, force_new=req.force_new)
    messages = repo.messages(conv.id)
    return {
        "id": conv.id,
        "scope_type": conv.scope_type,
        "scope_id": conv.scope_id,
        "title": conv.title,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "context_document_ids": m.context_document_ids,
                "ingest_date": m.ingest_date.isoformat(),
            }
            for m in messages
        ],
    }


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: int,
    req: MessageRequest,
    db: Session = Depends(get_db),
):
    """Stream an AI answer for the given conversation. Returns text/event-stream."""
    repo = ChatRepository(db)
    conv = repo.get(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Message content is empty")

    return StreamingResponse(
        stream_answer(conv, req.content.strip(), db),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
