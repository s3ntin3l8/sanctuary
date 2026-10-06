"""Slicing review: splitting a scanned multi-page PDF into documents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SlicingPage(BaseModel):
    page: int
    text_head: str
    text_tail: str
    has_thumbnail: bool


class ProposedCut(BaseModel):
    page: int
    confidence: Literal["high", "medium", "low"] | None
    notes: str | None


class SlicingView(BaseModel):
    batch_id: int
    subject: str | None
    status: Literal["preparing", "ready", "failed", "done"]
    page_count: int
    pages: list[SlicingPage]
    proposed_cuts: list[ProposedCut]
    error: str | None


class SlicingConfirm(BaseModel):
    """Split after each listed page (1 ≤ cut < page_count)."""

    cuts: list[int] = Field(default_factory=list)


class SlicingConfirmed(BaseModel):
    document_ids: list[int]
