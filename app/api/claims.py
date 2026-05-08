from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS
from app.dependencies import get_db
from app.models.database import (
    Case,
    Claim,
    ClaimEvidence,
    ClaimMergeProposal,
    Document,
)
from app.models.enums import ClaimEvidenceRole, ClaimStatus, UserReactionType
from app.services import claim_proposal_service as proposal_svc
from app.services.claim_service import ClaimRow, ClaimService


def _claim_belongs_to_case(db: Session, claim: Claim, case_id: str) -> bool:
    """A claim belongs to a case iff it has at least one ClaimEvidence row
    whose document is in that case. Replaces the old `claim.case_id == X`
    check after Wave 2A made claims global."""
    return (
        db.query(ClaimEvidence)
        .join(Document, Document.id == ClaimEvidence.document_id)
        .filter(
            ClaimEvidence.claim_id == claim.id,
            Document.case_id == case_id,
        )
        .first()
        is not None
    )


def _primary_case_for_claim(db: Session, claim: Claim) -> str | None:
    """Pick a case to render this claim in. Prefer the ASSERTS evidence row's
    document case (the canonical "originated by" anchor); fall back to any
    evidence row with a case."""
    asserts = (
        db.query(Document.case_id)
        .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
        .filter(
            ClaimEvidence.claim_id == claim.id,
            ClaimEvidence.role == ClaimEvidenceRole.ASSERTS,
            Document.case_id.isnot(None),
        )
        .first()
    )
    if asserts and asserts[0] and asserts[0] != "_TRIAGE":
        return asserts[0]
    any_evidence = (
        db.query(Document.case_id)
        .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
        .filter(
            ClaimEvidence.claim_id == claim.id,
            Document.case_id.isnot(None),
        )
        .first()
    )
    if any_evidence and any_evidence[0]:
        return any_evidence[0]
    return None


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

    case_id = _primary_case_for_claim(db, claim)
    case = db.query(Case).filter(Case.id == case_id).first() if case_id else None
    if case is None:
        # Claim has no evidence in any real case (only in _TRIAGE or none at
        # all). Render a degraded card via fallback ClaimRow rather than 500.
        return HTMLResponse(
            templates.get_template("components/claim_card.html").render(
                {
                    "row": ClaimRow(claim=claim),
                    "case": None,
                    "originator_colors": ORIGINATOR_COLORS,
                    "ClaimStatus": ClaimStatus,
                    "ClaimEvidenceRole": ClaimEvidenceRole,
                    "UserReactionType": UserReactionType,
                }
            )
        )

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


@router.post("/cases/{case_id}/claims/find-duplicates")
async def find_duplicates_in_case(
    request: Request,
    case_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Wave 2C: run the dedup judge over every claim in this case to
    surface historical duplicates. Returns an HTML status fragment for
    the Truth Map view to swap in."""
    if db.query(Case).filter(Case.id == case_id).first() is None:
        return HTMLResponse("<p>Case not found</p>", status_code=404)

    from app.services.intelligence.claim_dedup_judge import find_duplicates_for_case

    stats = find_duplicates_for_case(case_id, db)
    db.commit()

    pending_count = (
        db.query(ClaimMergeProposal)
        .join(ClaimEvidence, ClaimEvidence.claim_id == ClaimMergeProposal.new_claim_id)
        .join(Document, Document.id == ClaimEvidence.document_id)
        .filter(
            Document.case_id == case_id,
            ClaimMergeProposal.status == "PENDING",
        )
        .distinct()
        .count()
    )

    return templates.TemplateResponse(
        request,
        "partials/find_duplicates_result.html",
        {
            "case": db.query(Case).filter(Case.id == case_id).first(),
            "stats": stats,
            "pending_count": pending_count,
        },
    )


@router.post("/claims/proposals/merge/{proposal_id}/confirm")
async def confirm_merge_proposal(
    proposal_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Apply a pending ClaimMergeProposal: collapse new claim into existing."""
    prop = proposal_svc.confirm_merge(proposal_id, db)
    if prop is None:
        return HTMLResponse("<p>Proposal not found</p>", status_code=404)
    db.commit()
    return HTMLResponse("", status_code=200)


@router.post("/claims/proposals/merge/{proposal_id}/dismiss")
async def dismiss_merge_proposal(
    proposal_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Dismiss a pending ClaimMergeProposal without applying it."""
    prop = proposal_svc.dismiss_merge(proposal_id, db)
    if prop is None:
        return HTMLResponse("<p>Proposal not found</p>", status_code=404)
    db.commit()
    return HTMLResponse("", status_code=200)


@router.post("/claims/proposals/evidence/{proposal_id}/confirm")
async def confirm_evidence_proposal(
    proposal_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Apply a pending ClaimEvidenceProposal: write evidence row + transition status."""
    prop = proposal_svc.confirm_evidence(proposal_id, db)
    if prop is None:
        return HTMLResponse("<p>Proposal not found</p>", status_code=404)
    db.commit()
    return HTMLResponse("", status_code=200)


@router.post("/claims/proposals/evidence/{proposal_id}/dismiss")
async def dismiss_evidence_proposal(
    proposal_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Dismiss a pending ClaimEvidenceProposal."""
    prop = proposal_svc.dismiss_evidence(proposal_id, db)
    if prop is None:
        return HTMLResponse("<p>Proposal not found</p>", status_code=404)
    db.commit()
    return HTMLResponse("", status_code=200)


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
    if claim is None or not _claim_belongs_to_case(db, claim, case_id):
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
