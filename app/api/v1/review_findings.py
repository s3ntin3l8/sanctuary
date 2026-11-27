"""Second-opinion findings on AI output: list them, confirm or dismiss one, and
ask the opt-in verifier for them.

Findings are written by agents over MCP (``review_output``) and by the verifier
model (``POST .../verify``, and after CLAIMS when it is on); only the user
resolves them, here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.access_guards import (
    check_owned_or_case_access,
    require_case_access,
    require_document_access,
    require_review_finding_access,
)
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import AiReviewFinding, Case, Document, User
from app.models.enums import ReviewFindingStatus
from app.schemas.review_findings import ReviewFindingView, VerifierStatus, VerifyQueued
from app.services import access_service, ai_review_service
from app.services.ai_config import DOCUMENT_VERIFIER_GROUPS, verifier_for_case

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


# --- Verifier ---------------------------------------------------------------------
#
# Availability is per case: the verifier is off, unconfigured or out of scope
# for most. The SPA shows "Verify with <model>" only where it is available.


def _status(gate) -> VerifierStatus:
    return VerifierStatus(
        available=gate is not None, model=gate[0].model if gate else None
    )


@router.get("/documents/{doc_id}/verifier", response_model=VerifierStatus)
def document_verifier(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    doc: Document = Depends(require_document_access()),
):
    """Whether this user may ask the verifier to check this document."""
    if not check_owned_or_case_access(
        db, user, owner_id=doc.owner_id, case_id=doc.case_id, edit=True
    ):
        return _status(None)
    return _status(verifier_for_case(db, doc.case_id, DOCUMENT_VERIFIER_GROUPS))


@router.get("/cases/{case_id}/verifier", response_model=VerifierStatus)
def case_verifier(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access()),
):
    """Whether this user may ask the verifier to check this case."""
    if not access_service.can_edit_case(db, user, case):
        return _status(None)
    return _status(verifier_for_case(db, case.id))


def _unavailable() -> ApiError:
    return ApiError(
        409,
        "verifier_unavailable",
        "The verifier is off or this case is not in its scope.",
    )


@router.post("/documents/{doc_id}/verify", response_model=VerifyQueued, status_code=202)
@limiter.limit("10/minute")
def verify_document(
    request: Request,
    db: Session = Depends(get_db),
    doc: Document = Depends(require_document_access(edit=True)),
):
    """Queue the verifier's check of this document's AI output (sends its text
    to the verifier's endpoint)."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.verify import verify_document_task

    if verifier_for_case(db, doc.case_id, DOCUMENT_VERIFIER_GROUPS) is None:
        raise _unavailable()
    dispatch_task(verify_document_task, doc.id)
    return VerifyQueued(queued=True)


@router.post("/cases/{case_id}/verify", response_model=VerifyQueued, status_code=202)
@limiter.limit("5/minute")
def verify_case(
    request: Request,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Queue the verifier's check of the case brief and every document filed in
    the case (each sends its text to the verifier's endpoint)."""
    from app.tasks.dispatch import dispatch_task
    from app.tasks.verify import verify_case_task

    if verifier_for_case(db, case.id) is None:
        raise _unavailable()
    dispatch_task(verify_case_task, case.id)
    return VerifyQueued(queued=True)
