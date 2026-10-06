"""Chat conversations scoped to a document or a case."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ScopeType = Literal["document", "case"]


class ConversationSummary(BaseModel):
    id: int
    title: str | None
    created_at: datetime


class ChatMessage(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    context_document_ids: list[int] | None
    created_at: datetime


class ConversationDetail(BaseModel):
    id: int
    scope_type: ScopeType
    scope_id: str
    title: str | None
    messages: list[ChatMessage]


class ConversationOpen(BaseModel):
    scope_type: ScopeType
    scope_id: str = Field(min_length=1, max_length=64)
    force_new: bool = False


class ConversationTitle(BaseModel):
    title: str = Field(max_length=200)

    @field_validator("title")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Title must not be empty")
        return value


class MessageSend(BaseModel):
    content: str = Field(max_length=20_000)
    proceeding_id: int | None = None

    @field_validator("content")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message content is empty")
        return value


class Citation(BaseModel):
    """One ``[DOC:n#p=k]`` reference the assistant made, resolved to a passage."""

    doc_id: int
    case_id: str | None
    title: str
    passage_id: str | None
