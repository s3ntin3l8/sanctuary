"""Document- and case-scoped AI conversations.

``POST .../messages`` streams server-sent events: ``{"type":"token","t":…}``,
``{"type":"citations","docs":[Citation…]}`` and finally ``{"type":"done"}``.
"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.access_guards import check_scope_access, require_conversation_access
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import Conversation, User
from app.repositories.chat import ChatRepository
from app.schemas.chat import (
    ChatMessage,
    ConversationDetail,
    ConversationOpen,
    ConversationSummary,
    ConversationTitle,
    MessageSend,
)
from app.services.chat.chat_service import stream_answer

router = APIRouter(prefix="/chat", tags=["chat"])


def _detail(db: Session, conv: Conversation) -> ConversationDetail:
    return ConversationDetail(
        id=conv.id,
        scope_type=conv.scope_type,  # type: ignore[arg-type]
        scope_id=conv.scope_id,
        title=conv.title,
        messages=[
            ChatMessage(
                id=m.id,
                role=m.role,  # type: ignore[arg-type]
                content=m.content,
                context_document_ids=m.context_document_ids,
                created_at=m.ingest_date,
            )
            for m in ChatRepository(db).messages(conv.id)
        ],
    )


def _require_scope(db: Session, user: User, scope_type: str, scope_id: str) -> None:
    if not check_scope_access(db, user, scope_type, scope_id):
        raise ApiError(404, "not_found", "Not found.")


@router.get("/conversations", response_model=list[ConversationSummary])
def list_conversations(
    scope_type: str,
    scope_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The caller's own conversations for one document or case, newest first."""
    if scope_type not in ("document", "case"):
        raise ApiError(422, "bad_scope", "scope_type must be 'document' or 'case'.")
    _require_scope(db, user, scope_type, scope_id)
    return [
        ConversationSummary(id=c.id, title=c.title, created_at=c.ingest_date)
        for c in ChatRepository(db).list_by_scope(scope_type, scope_id, user.id)
    ]


@router.post("/conversations", response_model=ConversationDetail)
def open_conversation(
    body: ConversationOpen,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The caller's latest conversation for the scope, or a new one."""
    _require_scope(db, user, body.scope_type, body.scope_id)
    conv = ChatRepository(db).get_or_create(
        body.scope_type, body.scope_id, user.id, force_new=body.force_new
    )
    return _detail(db, conv)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    db: Session = Depends(get_db),
    conv: Conversation = Depends(require_conversation_access()),
):
    return _detail(db, conv)


@router.put(
    "/conversations/{conversation_id}/title", response_model=ConversationSummary
)
def rename_conversation(
    body: ConversationTitle,
    db: Session = Depends(get_db),
    conv: Conversation = Depends(require_conversation_access()),
):
    ChatRepository(db).update_title(conv.id, body.title)
    db.refresh(conv)
    return ConversationSummary(
        id=conv.id, title=conv.title, created_at=conv.ingest_date
    )


@router.delete(
    "/conversations/{conversation_id}", status_code=204, response_class=Response
)
def delete_conversation(
    db: Session = Depends(get_db),
    conv: Conversation = Depends(require_conversation_access()),
):
    ChatRepository(db).delete(conv.id)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
@limiter.limit("30/minute")
async def send_message(
    request: Request,
    body: MessageSend,
    db: Session = Depends(get_db),
    conv: Conversation = Depends(require_conversation_access()),
):
    """Stream the assistant's answer as server-sent events."""
    return StreamingResponse(
        stream_answer(conv, body.content, db, proceeding_id=body.proceeding_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
