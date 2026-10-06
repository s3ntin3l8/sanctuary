"""Cases directory, case creation and the AI close-suggestion decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.access_guards import require_case_access
from app.api.v1.errors import ApiError
from app.constants import CASE_STATUS_META
from app.dependencies import get_current_user, get_db
from app.models.database import Case, Proceeding, User
from app.models.enums import CaseStatus, ProceedingStatus
from app.schemas.cases import (
    CaseCard,
    CaseCreate,
    CaseCreated,
    CasesDirectory,
    NextAction,
)
from app.services.case_service import CaseIdTaken, CaseService

router = APIRouter(prefix="/cases", tags=["cases"])


def case_cards(
    service: CaseService,
    enriched: list[dict[str, Any]],
    *,
    doc_counts: dict[str, int] | None = None,
    action_counts: dict[str, int] | None = None,
) -> list[CaseCard]:
    """Turn ``CaseService.enrich_case_for_card`` dicts into API cards.

    Pass the count dicts when the caller already has them (the directory
    service computes both) to avoid repeating the two bulk queries.
    """
    ids = [c["id"] for c in enriched]
    if doc_counts is None:
        doc_counts = service.doc_repo.bulk_count_by_case(ids)
    if action_counts is None:
        action_counts = service.action_repo.bulk_count_open_by_case(ids)
    cards = []
    for c in enriched:
        action = c["next_action"]
        proceeding = c["active_proceeding"]
        cards.append(
            CaseCard(
                id=c["id"],
                title=c["title"],
                status=c["status"],
                status_label=CASE_STATUS_META.get(c["status"], {}).get(
                    "label", c["status"].value
                ),
                is_draft=c["is_draft"],
                pending_close=c["pending_close"],
                client_name=c["client_name"],
                opposing_party=c["opposing_party"],
                proceeding_name=c["proceeding_name"],
                matter_type=proceeding["matter_type"] if proceeding else "",
                next_action=NextAction(
                    title=action.title,
                    due_date=action.due_date,
                    action_type=action.action_type,
                )
                if action
                else None,
                exposure_eur=c["exposure_eur"],
                doc_count=doc_counts.get(c["id"], 0),
                open_action_count=action_counts.get(c["id"], 0),
                new_docs=c["new_docs"],
                days_since_activity=c["days_since_activity"],
                is_dormant=c["is_dormant"],
                max_significance=c["max_significance"],
                last_activity_at=c["updated_at"],
            )
        )
    return cards


@router.get("", response_model=CasesDirectory)
def cases_directory(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    service = CaseService(db)
    data = service.get_all_cases_directory(user.id)
    return CasesDirectory(
        cases=case_cards(
            service,
            data["cases"],
            doc_counts=data["doc_counts"],
            action_counts=data["deadline_counts"],
        ),
        counts_by_status=data["stats_by_status"],
        total=data["total"],
    )


@router.post("", response_model=CaseCreated, status_code=201)
def create_case(
    body: CaseCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a case and its initial active proceeding."""
    try:
        case = CaseService(db).create_case_with_proceeding(
            case_id=body.case_id.strip(),
            title=body.title.strip(),
            court_name=body.court_name.strip(),
            jurisdiction=body.jurisdiction,
            owner_id=user.id,
        )
    except CaseIdTaken as exc:
        raise ApiError(409, "case_id_taken", str(exc)) from exc
    return CaseCreated(id=case.id)


@router.post("/{case_id}/confirm-close", status_code=204, response_class=Response)
def confirm_close(
    case_id: str,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Accept an AI close suggestion: close the case and all its proceedings."""
    case.status = CaseStatus.CLOSED
    case.closed_at = datetime.now(UTC)
    case.pending_close = False
    db.query(Proceeding).filter(Proceeding.case_id == case_id).update(
        {"status": ProceedingStatus.CLOSED}
    )
    db.commit()


@router.post("/{case_id}/dismiss-close", status_code=204, response_class=Response)
def dismiss_close(
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Reject an AI close suggestion: clear the flag, keep the status."""
    case.pending_close = False
    if case.ai_brief and isinstance(case.ai_brief, dict):
        brief = dict(case.ai_brief)
        brief.pop("close_suggestion_rationale", None)
        case.ai_brief = brief
    db.commit()
