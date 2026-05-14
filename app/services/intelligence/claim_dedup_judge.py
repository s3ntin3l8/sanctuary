"""Wave 2B: dedup judge that reads a freshly-extracted claim and the top-K
embedding-nearest existing claims, then asks an LLM whether the candidate
restates one of them. High-confidence "merge" verdicts become
ClaimMergeProposal rows; the user confirms or dismisses before the merge
is applied.

The judge runs once per (candidate claim, nearest existing claim) pair —
typically K=5 calls per new claim. Each call is small and constrained;
the cost is dominated by the embedding lookup, not the judging.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.database import Claim, ClaimEvidence, ClaimMergeProposal
from app.models.enums import ClaimEvidenceRole, ProposalConfidence, ProposalStatus
from app.services.ai_config import get_chat_config
from app.services.claim_embedding import nearest_claims
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.prompts import CLAIM_DEDUP_JUDGE_SYSTEM
from app.services.intelligence.schemas import ClaimDedupJudgement

logger = logging.getLogger(__name__)

JUDGE_OPTIONS = {"temperature": 0.0, "num_predict": 200}


def _judge_pair(
    candidate_text: str,
    existing_id: int,
    existing_text: str,
    model: str,
    db: Session,
) -> ClaimDedupJudgement | None:
    user_prompt = (
        f"CANDIDATE CLAIM (newly extracted):\n{candidate_text}\n\n"
        f"NEAREST EXISTING CLAIM (id={existing_id}):\n{existing_text}"
    )
    try:
        return call_json_ai(
            system_prompt=CLAIM_DEDUP_JUDGE_SYSTEM,
            user_prompt=user_prompt,
            options=JUDGE_OPTIONS,
            debug_label=f"dedup_judge_{existing_id}",
            schema=ClaimDedupJudgement,
            model=model or None,
            db=db,
            two_pass=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedup judge call failed for existing %s: %s", existing_id, exc)
        return None


def propose_merges_for_new_claim(
    new_claim: Claim,
    db: Session,
    *,
    case_id: str | None = None,
    k: int = 5,
) -> list[ClaimMergeProposal]:
    """Compare `new_claim` against the top-K nearest existing claims and
    create a ClaimMergeProposal for each high-confidence match.

    Commits after each proposal so SQLite's WAL write lock isn't held
    across the slow AI judge calls — otherwise concurrent Celery writers
    (extract / enrich tasks) hit `database is locked` past busy_timeout.

    Returns the proposals created (empty list if none).
    """
    cfg = get_chat_config(db)
    nearest = asyncio.run(
        nearest_claims(
            new_claim.claim_text,
            db,
            k=k,
            case_id=case_id,
            exclude_claim_id=new_claim.id,
        )
    )
    if not nearest:
        return []

    # Pre-compute the set of document ids that ASSERT this new_claim.
    # If the matched existing claim ALSO has an ASSERTS row from one of these
    # documents, the LLM produced two near-identical claims from the same
    # source document — auto-apply the merge instead of asking the user, since
    # there is no user signal worth preserving in an intra-doc duplicate.
    new_claim_doc_ids = {
        ev.document_id
        for ev in db.query(ClaimEvidence)
        .filter(
            ClaimEvidence.claim_id == new_claim.id,
            ClaimEvidence.role == ClaimEvidenceRole.ASSERTS,
        )
        .all()
    }

    proposals: list[ClaimMergeProposal] = []
    confidence_map = {
        "high": ProposalConfidence.HIGH,
        "medium": ProposalConfidence.MEDIUM,
        "low": ProposalConfidence.LOW,
    }
    for existing_id, _distance in nearest:
        existing = db.get(Claim, existing_id)
        if not existing:
            continue
        verdict = _judge_pair(
            new_claim.claim_text,
            existing_id,
            existing.claim_text,
            cfg.summary_model,
            db,
        )
        if verdict is None or verdict.action != "merge":
            continue

        # Intra-document duplicate auto-merge. If both claims are ASSERTED by
        # the same document, the LLM produced a duplicate within one extraction
        # pass — collapse it now without bothering the user.
        existing_doc_ids = {
            ev.document_id
            for ev in db.query(ClaimEvidence)
            .filter(
                ClaimEvidence.claim_id == existing_id,
                ClaimEvidence.role == ClaimEvidenceRole.ASSERTS,
            )
            .all()
        }
        intra_doc = bool(new_claim_doc_ids & existing_doc_ids)
        if intra_doc and verdict.confidence in ("high", "medium"):
            from app.services.claim_proposal_service import confirm_merge

            auto_prop = ClaimMergeProposal(
                new_claim_id=new_claim.id,
                existing_claim_id=existing_id,
                confidence=confidence_map.get(
                    verdict.confidence, ProposalConfidence.LOW
                ),
                rationale=f"intra-doc auto-merge: {verdict.rationale}",
                status=ProposalStatus.PENDING,
                proposed_at=datetime.now(UTC).replace(tzinfo=None),
            )
            db.add(auto_prop)
            db.flush()
            confirm_merge(auto_prop.id, db)
            db.commit()
            logger.info(
                "auto-merged intra-doc duplicate: new claim %s collapsed into existing %s",
                new_claim.id,
                existing_id,
            )
            # new_claim has been deleted by confirm_merge; stop iterating.
            return proposals

        prop = ClaimMergeProposal(
            new_claim_id=new_claim.id,
            existing_claim_id=existing_id,
            confidence=confidence_map.get(verdict.confidence, ProposalConfidence.LOW),
            rationale=verdict.rationale,
            status=ProposalStatus.PENDING,
            proposed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(prop)
        db.commit()  # release the write lock before the next judge call
        proposals.append(prop)
        logger.info(
            "merge proposal: new claim %s ≈ existing %s (%s confidence)",
            new_claim.id,
            existing_id,
            verdict.confidence,
        )
        # First high-confidence match is enough; don't pile up duplicate
        # proposals for the same claim. User can manually pick others later.
        if verdict.confidence == "high":
            break

    return proposals


async def find_duplicates_for_case(
    case_id: str,
    db: Session,
    *,
    k: int = 3,
    skip_pair_if_proposal_exists: bool = True,
) -> dict[str, int]:
    """Wave 2C: retroactive duplicate-finder. Iterates every claim in the
    case, runs the dedup judge against each claim's top-K nearest, and
    creates ClaimMergeProposal rows for high-confidence merges.

    Idempotent: by default, skips (claim_a, claim_b) pairs that already
    have an active proposal in either direction.

    Returns {scanned, proposals_created, judge_calls} for UI feedback.
    """
    from app.models.database import ClaimMergeProposal
    from app.repositories.claim import ClaimRepository

    cfg = get_chat_config(db)
    repo = ClaimRepository(db)
    claims = list(repo.claims_for_case(case_id))
    if not claims:
        return {"scanned": 0, "proposals_created": 0, "judge_calls": 0}

    # Pre-load existing proposals (any status) keyed by unordered pair.
    existing_pairs: set[tuple[int, int]] = set()
    if skip_pair_if_proposal_exists:
        rows = db.query(
            ClaimMergeProposal.new_claim_id,
            ClaimMergeProposal.existing_claim_id,
        ).all()
        for a, b in rows:
            existing_pairs.add(tuple(sorted((a, b))))

    confidence_map = {
        "high": ProposalConfidence.HIGH,
        "medium": ProposalConfidence.MEDIUM,
        "low": ProposalConfidence.LOW,
    }
    proposals_created = 0
    judge_calls = 0
    seen_pairs_this_run: set[tuple[int, int]] = set()

    for claim in claims:
        nearest = await nearest_claims(
            claim.claim_text,
            db,
            k=k,
            case_id=case_id,
            exclude_claim_id=claim.id,
        )
        for existing_id, _distance in nearest:
            pair = tuple(sorted((claim.id, existing_id)))
            if pair in existing_pairs or pair in seen_pairs_this_run:
                continue
            seen_pairs_this_run.add(pair)

            existing = db.get(Claim, existing_id)
            if not existing:
                continue
            judge_calls += 1
            verdict = _judge_pair(
                claim.claim_text,
                existing_id,
                existing.claim_text,
                cfg.summary_model,
                db,
            )
            if verdict is None or verdict.action != "merge":
                continue

            # Pick a deterministic "new" side: the higher-id claim
            # (typically the more recently extracted one). The user can
            # always swap directionality manually in the queue.
            new_id, existing_target_id = (
                (claim.id, existing_id)
                if claim.id > existing_id
                else (existing_id, claim.id)
            )
            db.add(
                ClaimMergeProposal(
                    new_claim_id=new_id,
                    existing_claim_id=existing_target_id,
                    confidence=confidence_map.get(
                        verdict.confidence, ProposalConfidence.LOW
                    ),
                    rationale=verdict.rationale,
                    status=ProposalStatus.PENDING,
                    proposed_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            # Commit per proposal so the SQLite write lock isn't held across
            # the next slow judge call. Without this, concurrent Celery
            # writers hit `database is locked` past the 5s busy_timeout.
            db.commit()
            proposals_created += 1
            existing_pairs.add(pair)
            logger.info(
                "retroactive merge proposal: claim %s ≈ %s (%s)",
                new_id,
                existing_target_id,
                verdict.confidence,
            )

    return {
        "scanned": len(claims),
        "proposals_created": proposals_created,
        "judge_calls": judge_calls,
    }
