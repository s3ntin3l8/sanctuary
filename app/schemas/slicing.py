"""Slicing review: splitting a scanned multi-page PDF into documents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SlicingPage(BaseModel):
    page: int
    text_head: str
    text_tail: str
    has_thumbnail: bool


CutKind = Literal["letter", "attachment"]


class ProposedCut(BaseModel):
    page: int
    confidence: Literal["high", "medium", "low"] | None
    kind: CutKind
    notes: str | None


class SlicingView(BaseModel):
    batch_id: int
    subject: str | None
    status: Literal["preparing", "ready", "failed", "done"]
    page_count: int
    pages: list[SlicingPage]
    proposed_cuts: list[ProposedCut]
    error: str | None
    progress_done: int | None = None
    progress_total: int | None = None
    progress_phase: Literal["ocr", "ai"] | None = None


class SliceCut(BaseModel):
    """Split after ``page`` (1 ≤ page < page_count); ``kind`` says what the next
    part is: a new letter, or an attachment of the letter before it."""

    page: int
    kind: CutKind


class SlicingConfirm(BaseModel):
    cuts: list[SliceCut] = Field(default_factory=list)


class SlicingConfirmed(BaseModel):
    document_ids: list[int]
