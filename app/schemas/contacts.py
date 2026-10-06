"""A correspondent (document sender) and everything they sent."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.enums import CaseStatus, OriginatorType


class ContactCase(BaseModel):
    id: str
    title: str
    status: CaseStatus


class ContactDocument(BaseModel):
    id: int
    title: str
    case_id: str | None
    case_title: str | None
    originator_type: OriginatorType
    issued_date: datetime | None
    ingest_date: datetime | None
    legal_significance: str | None


class ContactView(BaseModel):
    name: str
    document_count: int
    case_count: int
    last_contact: datetime | None
    cases: list[ContactCase]
    documents: list[ContactDocument]
