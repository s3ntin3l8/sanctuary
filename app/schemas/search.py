"""Command-palette search results."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.enums import CaseStatus


class SearchDocument(BaseModel):
    id: int
    title: str
    case_id: str | None


class SearchCase(BaseModel):
    id: str
    title: str
    status: CaseStatus


class SearchContact(BaseModel):
    name: str


class SearchResults(BaseModel):
    documents: list[SearchDocument]
    cases: list[SearchCase]
    contacts: list[SearchContact]
    total: int
