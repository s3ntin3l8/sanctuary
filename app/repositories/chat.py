from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import Conversation, ConversationMessage


class ChatRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(
        self, scope_type: str, scope_id: str, force_new: bool = False
    ) -> Conversation:
        """Return the most recent conversation for this scope, or create one."""
        if not force_new:
            conv = (
                self.db.query(Conversation)
                .filter(
                    Conversation.scope_type == scope_type,
                    Conversation.scope_id == scope_id,
                )
                .order_by(Conversation.ingest_date.desc())
                .first()
            )
            if conv:
                return conv
        conv = Conversation(
            scope_type=scope_type,
            scope_id=scope_id,
            ingest_date=datetime.now(),
        )
        self.db.add(conv)
        self.db.commit()
        self.db.refresh(conv)
        return conv

    def get(self, conversation_id: int) -> Conversation | None:
        return self.db.get(Conversation, conversation_id)

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        context_document_ids: list[int] | None = None,
    ) -> ConversationMessage:
        msg = ConversationMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            context_document_ids=context_document_ids,
            ingest_date=datetime.now(),
        )
        self.db.add(msg)
        self.db.commit()
        self.db.refresh(msg)
        return msg

    def messages(self, conversation_id: int) -> list[ConversationMessage]:
        return (
            self.db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.ingest_date.asc())
            .all()
        )

    def list_by_scope(self, scope_type: str, scope_id: str) -> list[Conversation]:
        """List all conversations for a given scope."""
        return (
            self.db.query(Conversation)
            .filter(
                Conversation.scope_type == scope_type,
                Conversation.scope_id == scope_id,
            )
            .order_by(Conversation.ingest_date.desc())
            .all()
        )

    def delete(self, conversation_id: int) -> bool:
        """Delete a conversation and its messages. Returns True if found."""
        conv = self.get(conversation_id)
        if not conv:
            return False
        self.db.delete(conv)
        self.db.commit()
        return True

    def update_title(self, conversation_id: int, title: str) -> Conversation | None:
        """Update conversation title."""
        conv = self.get(conversation_id)
        if conv:
            conv.title = title
            self.db.commit()
            self.db.refresh(conv)
        return conv
