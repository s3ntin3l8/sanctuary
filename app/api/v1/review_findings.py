"""Second-opinion findings on AI output: list them, confirm or dismiss one.

Findings are written by agents over MCP (``review_output``) and later by the
cloud verifier; only the user resolves them, here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.access_guards import (
    require_case_access,
    require_document_access,
    require_review_finding_access,
)
from app.api.v1.errors import ApiError
from app.dependencies import get_current_user, get_db
from app.models.database import AiReviewFinding, Case, Document, User
from app.models.enums import ReviewFindingStatus
from app.schemas.review_findings import ReviewFindingView
from app.services import ai_review_service

router = APIRouter(tags=["review-findings"])


@router.get(
    "/documents/{doc_id}/review-findings", response_model=list[ReviewFindingView]
)
def document_findings(
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access()),
):
    return ai_review_service.views(db, ai_review_service.for_document(db, doc.id))


@router.get("/cases/{case_id}/review-findings", response_model=list[ReviewFindingView])
def case_findings(
    status: ReviewFindingStatus | None = Query(None),
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access()),
):
    return ai_review_service.views(db, ai_review_service.for_case(db, case.id, status))


@router.post("/review-findings/{finding_id}/confirm", response_model=ReviewFindingView)
def confirm_finding(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    finding: AiReviewFinding = Depends(require_review_finding_access(edit=True)),
):
    """Accept the finding; a disagreement's proposed value is applied."""
    try:
        done = ai_review_service.confirm(db, user, finding)
    except ai_review_service.ReviewFindingError as exc:
        raise ApiError(exc.status, exc.code, exc.detail) from exc
    return ai_review_service.views(db, [done])[0]


@router.post("/review-findings/{finding_id}/dismiss", response_model=ReviewFindingView)
def dismiss_finding(
    db: Session = Depends(get_db),
    finding: AiReviewFinding = Depends(require_review_finding_access(edit=True)),
):
    """Reject the finding; the AI output stays as it is."""
    try:
        done = ai_review_service.dismiss(db, finding)
    except ai_review_service.ReviewFindingError as exc:
        raise ApiError(exc.status, exc.code, exc.detail) from exc
    return ai_review_service.views(db, [done])[0]
