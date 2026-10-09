"""The Truth Map: claims, their evidence chain, and AI merge/evidence proposals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.access_guards import (
    merge_proposal_access_allowed,
    require_case_access,
    require_claim_access,
    require_evidence_proposal_access,
    require_merge_proposal_access,
)
from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import (
    Case,
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    Document,
    User,
)
from app.repositories.claim import ClaimRepository
from app.schemas.case_detail import (
    ClaimGroupView,
    ClaimStatusUpdate,
    ClaimView,
    DedupJob,
    EvidenceProposalView,
    EvidenceView,
    MergeProposalView,
    ProposalBatch,
    ProposalBatchResult,
    TruthMapFilter,
    TruthMapView,
)
from app.services import access_service
from app.services import claim_proposal_service as proposal_svc
from app.services import user_settings_service as uss
from app.services.claim_service import _USER_ALLOWED, ClaimRow, ClaimService

router = APIRouter(tags=["claims"])


def _claim_view(row: ClaimRow) -> ClaimView:
    c = row.claim
    return ClaimView(
        id=c.id,
        claim_text=c.claim_text,
        claim_type=c.claim_type,
        status=c.status,
        is_precedent=bool(c.is_precedent),
        first_made_at=c.first_made_at,
        last_updated_at=c.last_updated_at,
        allowed_transitions=sorted(
            _USER_ALLOWED.get(c.status, set()), key=lambda s: s.value
        ),
        evidence=[
            EvidenceView(
                id=e.evidence.id,
                role=e.evidence.role,
                excerpt=e.evidence.excerpt,
                confidence=e.evidence.confidence,
                document_id=e.document.id,
                document_title=e.document.title,
                document_originator=e.document.originator_type,
                document_date=e.document.issued_date or e.document.ingest_date,
                reactions=[r.reaction for r in e.reactions],
            )
            for e in row.evidence
        ],
    )


def _dedup_job(raw: dict | None) -> DedupJob | None:
    if not raw:
        return None
    stats = raw.get("stats") or {}
    return DedupJob(
        status=raw.get("status", "done"),
        processed=int(raw.get("processed") or stats.get("scanned") or 0),
        total=int(raw.get("total") or 0),
        proposals=int(stats.get("proposals_created") or 0),
        error=stats.get("error"),
    )


def _truth_map(
    db: Session, user: User, case_id: str, filter_: TruthMapFilter
) -> TruthMapView:
    view = ClaimService(db).get_truth_map(case_id, filter_, viewer=user)
    return TruthMapView(
        filter=filter_,
        groups=[
            ClaimGroupView(status=g.status, claims=[_claim_view(r) for r in g.claims])
            for g in view.groups
        ],
        open_claim_count=view.open_claim_count,
        pending_merges=[MergeProposalView(**p.__dict__) for p in view.pending_merges],
        pending_evidence=[
            EvidenceProposalView(**p.__dict__) for p in view.pending_evidence
        ],
        pipeline_active_doc_count=view.pipeline_active_doc_count,
        dedup_job=_dedup_job(uss.get_dedup_job(case_id, db)),
    )


@router.get("/cases/{case_id}/truthmap", response_model=TruthMapView)
def truth_map(
    filter: TruthMapFilter = Query("open"),
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access()),
    user: User = Depends(get_current_user),
):
    return _truth_map(db, user, case.id, filter)


# --- Claims ------------------------------------------------------------------


@router.put("/claims/{claim_id}/status", response_model=ClaimView)
def set_claim_status(
    body: ClaimStatusUpdate,
    db: Session = Depends(get_db),
    claim: Claim = Depends(require_claim_access(edit=True)),
    user: User = Depends(get_current_user),
):
    try:
        ClaimService(db).transition_status(claim.id, body.status)
    except ValueError as e:
        raise ApiError(422, "bad_transition", str(e)) from e
    db.commit()
    return _single_claim(db, user, claim.id)


@router.post("/claims/{claim_id}/precedent", response_model=ClaimView)
def toggle_precedent(
    db: Session = Depends(get_db),
    claim: Claim = Depends(require_claim_access(edit=True)),
    user: User = Depends(get_current_user),
):
    claim.is_precedent = not claim.is_precedent
    db.commit()
    return _single_claim(db, user, claim.id)


@router.delete("/claims/{claim_id}", status_code=204, response_class=Response)
def dismiss_claim(
    db: Session = Depends(get_db),
    claim: Claim = Depends(require_claim_access(edit=True)),
):
    ClaimService(db).dismiss_claim(claim.id)
    db.commit()


def _single_claim(db: Session, user: User, claim_id: int) -> ClaimView:
    """The claim with its evidence chain, as the truth map would show it."""
    claim = db.get(Claim, claim_id)
    assert claim is not None
    # Render through a case the caller can actually see — the claim may also be
    # evidenced from cases they cannot (#159).
    visible = access_service.visible_case_ids(db, user)
    case_ids = (
        db.query(Document.case_id)
        .join(ClaimEvidence, ClaimEvidence.document_id == Document.id)
        .filter(ClaimEvidence.claim_id == claim_id, Document.case_id != "_TRIAGE")
        .distinct()
        .order_by(Document.case_id)
        .all()
    )
    case_id = next(
        (c for (c,) in case_ids if c and (visible is None or c in visible)), None
    )
    # A claim evidenced only by the caller's own untriaged documents has no
    # visible real case; render it through the _TRIAGE bucket instead (the
    # viewer filter keeps only their own documents) so the evidence the
    # access guard promised is actually shown.
    view = ClaimService(db).get_truth_map(case_id or "_TRIAGE", "all", viewer=user)
    for g in view.groups:
        for row in g.claims:
            if row.claim.id == claim_id:
                return _claim_view(row)
    return _claim_view(ClaimRow(claim=claim))


# --- Proposals ---------------------------------------------------------------


@router.post(
    "/claims/proposals/merge/{proposal_id}/confirm",
    status_code=204,
    response_class=Response,
)
def confirm_merge(
    db: Session = Depends(get_db),
    proposal: ClaimMergeProposal = Depends(require_merge_proposal_access(edit=True)),
):
    proposal_svc.confirm_merge(proposal.id, db)
    db.commit()


@router.post(
    "/claims/proposals/merge/{proposal_id}/dismiss",
    status_code=204,
    response_class=Response,
)
def dismiss_merge(
    db: Session = Depends(get_db),
    proposal: ClaimMergeProposal = Depends(require_merge_proposal_access(edit=True)),
):
    proposal_svc.dismiss_merge(proposal.id, db)
    db.commit()


def _refresh_source_review(document_id: int, db: Session) -> None:
    """Resolving a CONTESTS/REFUTES proposal may clear the document's review flag.

    Runs inside the caller's transaction, so the proposal and the flag commit together.
    """
    from app.services.ingestion.service import refresh_review_reasons

    source = db.get(Document, document_id)
    if source:
        refresh_review_reasons(source, db, commit=False)


@router.post(
    "/claims/proposals/evidence/{proposal_id}/confirm",
    status_code=204,
    response_class=Response,
)
def confirm_evidence(
    db: Session = Depends(get_db),
    proposal: ClaimEvidenceProposal = Depends(
        require_evidence_proposal_access(edit=True)
    ),
):
    proposal_svc.confirm_evidence(proposal.id, db)
    _refresh_source_review(proposal.source_document_id, db)
    db.commit()


@router.post(
    "/claims/proposals/evidence/{proposal_id}/dismiss",
    status_code=204,
    response_class=Response,
)
def dismiss_evidence(
    db: Session = Depends(get_db),
    proposal: ClaimEvidenceProposal = Depends(
        require_evidence_proposal_access(edit=True)
    ),
):
    proposal_svc.dismiss_evidence(proposal.id, db)
    _refresh_source_review(proposal.source_document_id, db)
    db.commit()


@router.post(
    "/cases/{case_id}/claims/proposals/merge", response_model=ProposalBatchResult
)
def batch_merge(
    body: ProposalBatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Confirm or dismiss every pending merge proposal the caller may act on."""
    candidates = (
        db.query(ClaimMergeProposal)
        .join(ClaimEvidence, ClaimEvidence.claim_id == ClaimMergeProposal.new_claim_id)
        .join(Document, Document.id == ClaimEvidence.document_id)
        .filter(Document.case_id == case.id, ClaimMergeProposal.status == "PENDING")
        .distinct()
        .all()
    )
    # A merge can span a second case via the existing claim's evidence, so
    # editing this case is not enough: check each proposal.
    pending = [
        p.id
        for p in candidates
        if merge_proposal_access_allowed(db, user, p, edit=True)
    ]
    done = 0
    for pid in pending:
        # A preceding confirm may have cascade-deleted later proposals.
        db.expire_all()
        if db.get(ClaimMergeProposal, pid) is None:
            continue
        if body.action == "confirm":
            proposal_svc.confirm_merge(pid, db)
        else:
            proposal_svc.dismiss_merge(pid, db)
        done += 1
    db.commit()
    return ProposalBatchResult(
        confirmed=done if body.action == "confirm" else 0,
        dismissed=done if body.action == "dismiss" else 0,
    )


# --- Find duplicates ---------------------------------------------------------


@router.post("/cases/{case_id}/claims/find-duplicates", response_model=DedupJob)
@limiter.limit("5/minute")
def find_duplicates(
    request: Request,
    db: Session = Depends(get_db),
    case: Case = Depends(require_case_access(edit=True)),
):
    """Start the dedup judge as a background task; poll the truth map for progress."""
    from app.tasks.claim_dedup import claim_dedup_task
    from app.tasks.dispatch import dispatch_task

    existing = uss.get_dedup_job(case.id, db)
    if existing and existing.get("status") == "running":
        job = _dedup_job(existing)
        assert job is not None
        return job
    total = len(ClaimRepository(db).claims_for_case(case.id))
    uss.set_dedup_running(case.id, db, total=total)
    db.commit()
    dispatch_task(claim_dedup_task, case.id)
    job = _dedup_job(uss.get_dedup_job(case.id, db))
    assert job is not None
    return job
