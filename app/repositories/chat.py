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
                .order_by(Conversation.created_at.desc())
                .first()
            )
            if conv:
                return conv
        conv = Conversation(
            scope_type=scope_type,
            scope_id=scope_id,
            created_at=datetime.now(),
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
            created_at=datetime.now(),
        )
        self.db.add(msg)
        self.db.commit()
        self.db.refresh(msg)
        return msg

    def messages(self, conversation_id: int) -> list[ConversationMessage]:
        return (
            self.db.query(ConversationMessage)
            .filter(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.asc())
            .all()
        )
