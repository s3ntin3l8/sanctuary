"""Second-opinion findings on AI output, as the review panel and case page show them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models.enums import (
    ReviewerKind,
    ReviewFindingStatus,
    ReviewTargetKind,
    ReviewVerdict,
)


class ReviewFindingView(BaseModel):
    id: int
    reviewer_kind: ReviewerKind
    # Name of the agent token that wrote it (null for the verifier or a
    # since-deleted token).
    agent_token_name: str | None
    target_kind: ReviewTargetKind
    # What the finding is about, in words ("Significance tier", the claim text …).
    target_label: str
    document_id: int | None
    document_title: str | None
    case_id: str
    target_id: int | None
    field: str | None
    verdict: ReviewVerdict
    ai_value: Any
    proposed_value: Any
    rationale: str
    cited_excerpt: str | None
    reviewed_model: str | None
    reviewed_prompt_version: str | None
    status: ReviewFindingStatus
    created_at: datetime
    resolved_at: datetime | None


class VerifierStatus(BaseModel):
    """Whether the verifier may check this document or case, and with which model."""

    available: bool
    model: str | None


class VerifyQueued(BaseModel):
    queued: bool
