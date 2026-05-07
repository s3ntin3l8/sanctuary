from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS
from app.dependencies import get_db
from app.models.database import Case, Claim
from app.models.enums import ClaimEvidenceRole, ClaimStatus, UserReactionType
from app.services.claim_service import ClaimRow, ClaimService

router = APIRouter(tags=["claims"])


@router.get("/cases/{case_id}/truthmap")
async def get_truthmap(
    request: Request,
    case_id: str,
    filter: str = "open",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        return HTMLResponse("<p>Case not found</p>", status_code=404)

    if filter not in ("open", "established", "refuted", "all"):
        filter = "open"

    svc = ClaimService(db)
    truth_map = svc.get_truth_map(case_id, filter)  # type: ignore[arg-type]

    return templates.TemplateResponse(
        request,
        "partials/case_view_truthmap.html",
        {
            "case": case,
            "truth_map": truth_map,
            "originator_colors": ORIGINATOR_COLORS,
            "ClaimStatus": ClaimStatus,
            "ClaimEvidenceRole": ClaimEvidenceRole,
            "UserReactionType": UserReactionType,
        },
    )


@router.post("/claims/{claim_id}/precedent/toggle")
async def toggle_claim_precedent(
    request: Request,
    claim_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Toggle the ⚖️ Precedent flag on a claim. Independent of status."""
    claim = db.get(Claim, claim_id)
    if claim is None:
        return HTMLResponse("<p>Claim not found</p>", status_code=404)

    claim.is_precedent = not claim.is_precedent
    db.commit()
    db.refresh(claim)

    case = db.query(Case).filter(Case.id == claim.case_id).first()
    if case is None:
        # Should not happen while case_id remains on Claim; defensive only.
        return HTMLResponse("", status_code=204)

    svc = ClaimService(db)
    truth_map = svc.get_truth_map(case.id, "all")
    updated_row: ClaimRow | None = None
    for group in truth_map.groups:
        for row in group.claims:
            if row.claim.id == claim_id:
                updated_row = row
                break
    if updated_row is None:
        updated_row = ClaimRow(claim=claim)

    return HTMLResponse(
        templates.get_template("components/claim_card.html").render(
            {
                "row": updated_row,
                "case": case,
                "originator_colors": ORIGINATOR_COLORS,
                "ClaimStatus": ClaimStatus,
                "ClaimEvidenceRole": ClaimEvidenceRole,
                "UserReactionType": UserReactionType,
            }
        )
    )


@router.post("/cases/{case_id}/claims/{claim_id}/status")
async def update_claim_status(
    request: Request,
    case_id: str,
    claim_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if db.query(Case).filter(Case.id == case_id).first() is None:
        return HTMLResponse("<p>Case not found</p>", status_code=404)

    claim = db.get(Claim, claim_id)
    if claim is None or claim.case_id != case_id:
        return HTMLResponse("<p>Claim not found</p>", status_code=404)

    try:
        target = ClaimStatus(status)
    except ValueError:
        return HTMLResponse(f"Unknown status: {status}", status_code=422)

    svc = ClaimService(db)
    try:
        updated_claim = svc.transition_status(claim_id, target)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=422)

    db.commit()

    # Reload the truth map to get a fresh ClaimRow with evidence + reactions
    truth_map = svc.get_truth_map(case_id, "all")
    case = db.query(Case).filter(Case.id == case_id).first()

    # Find the updated row
    updated_row: ClaimRow | None = None
    for group in truth_map.groups:
        for row in group.claims:
            if row.claim.id == claim_id:
                updated_row = row
                break

    # Fallback: plain row if not found (shouldn't happen)
    if updated_row is None:
        updated_row = ClaimRow(claim=updated_claim)

    # Render the claim card
    card_html = templates.get_template("components/claim_card.html").render(
        {
            "row": updated_row,
            "case": case,
            "originator_colors": ORIGINATOR_COLORS,
            "ClaimStatus": ClaimStatus,
            "ClaimEvidenceRole": ClaimEvidenceRole,
            "UserReactionType": UserReactionType,
        }
    )

    # OOB swap to update the tab badge
    open_count = truth_map.open_claim_count
    badge_html = (
        f'<span id="truthmap-badge" hx-swap-oob="outerHTML:#truthmap-badge" '
        f'class="ml-1 text-[9px] font-bold px-1 rounded-full bg-primary/20 text-primary" '
        f'x-show="nodeCounts && nodeCounts.open_claims > 0" x-text="nodeCounts.open_claims">'
        f"{str(open_count) if open_count else ''}"
        f"</span>"
    )

    return HTMLResponse(content=card_html + badge_html)
