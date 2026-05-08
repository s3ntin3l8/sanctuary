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

from app.models.database import Claim, ClaimMergeProposal
from app.models.enums import ProposalConfidence, ProposalStatus
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
        prop = ClaimMergeProposal(
            new_claim_id=new_claim.id,
            existing_claim_id=existing_id,
            confidence=confidence_map.get(verdict.confidence, ProposalConfidence.LOW),
            rationale=verdict.rationale,
            status=ProposalStatus.PENDING,
            proposed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(prop)
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

    if proposals:
        db.flush()
    return proposals
