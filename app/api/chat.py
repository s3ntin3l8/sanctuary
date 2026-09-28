"""Chat API — document-scoped and case-scoped AI conversations."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.access_guards import check_scope_access, require_conversation_access
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import Conversation, User
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
    proceeding_id: int | None = None


class TitleRequest(BaseModel):
    title: str


@router.get("/conversations")
def list_conversations(
    scope_type: str,
    scope_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List the caller's own conversations for a given scope."""
    if scope_type not in ("document", "case"):
        raise HTTPException(
            status_code=400, detail="scope_type must be 'document' or 'case'"
        )
    if not check_scope_access(db, user, scope_type, scope_id):
        raise HTTPException(status_code=404, detail="Not found")
    repo = ChatRepository(db)
    convs = repo.list_by_scope(scope_type, scope_id, user.id)
    return [
        {
            "id": c.id,
            "title": c.title,
            "ingest_date": c.ingest_date.isoformat(),
        }
        for c in convs
    ]


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    conv: Conversation = Depends(require_conversation_access()),
):
    """Get a specific conversation by ID."""
    repo = ChatRepository(db)
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


@router.post("/conversations")
def get_or_create_conversation(
    req: ConversationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return the caller's active conversation for a scope, creating one if
    needed."""
    if req.scope_type not in ("document", "case"):
        raise HTTPException(
            status_code=400, detail="scope_type must be 'document' or 'case'"
        )
    if not check_scope_access(db, user, req.scope_type, req.scope_id):
        raise HTTPException(status_code=404, detail="Not found")
    repo = ChatRepository(db)
    conv = repo.get_or_create(
        req.scope_type, req.scope_id, user.id, force_new=req.force_new
    )
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


@router.post("/conversations/{conversation_id}/title")
def update_conversation_title(
    conversation_id: int,
    req: TitleRequest,
    db: Session = Depends(get_db),
    _conv: Conversation = Depends(require_conversation_access()),
):
    """Update conversation title."""
    repo = ChatRepository(db)
    conv = repo.update_title(conversation_id, req.title)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"id": conv.id, "title": conv.title}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    _conv: Conversation = Depends(require_conversation_access()),
):
    """Delete a conversation and all its messages."""
    repo = ChatRepository(db)
    if not repo.delete(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": conversation_id}


@router.post("/conversations/{conversation_id}/messages")
@limiter.limit("30/minute")
async def send_message(
    conversation_id: int,
    request: Request,
    req: MessageRequest,
    db: Session = Depends(get_db),
    conv: Conversation = Depends(require_conversation_access()),
):
    """Stream an AI answer for the given conversation. Returns text/event-stream."""
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Message content is empty")

    return StreamingResponse(
        stream_answer(conv, req.content.strip(), db, proceeding_id=req.proceeding_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
