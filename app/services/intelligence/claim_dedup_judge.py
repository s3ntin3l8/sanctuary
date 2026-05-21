"""Claim dedup judge — two entry points:

propose_merges_for_new_claim  (Wave 2B)
    Per-new-claim, real-time during extraction. Judges each embedding-nearest
    pair individually so intra-doc auto-merge logic can fire immediately.

find_duplicates_for_case  (Wave 2C)
    Retroactive case-wide scan. Collects all embedding-candidate pairs first,
    then judges them in a single batched LLM call rather than one call per pair.
    Batch size is capped at BATCH_SIZE to keep prompts manageable.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from app.core.timezone import naive_utc_now
from app.models.database import Claim, ClaimEvidence, ClaimMergeProposal
from app.models.enums import ClaimEvidenceRole, ProposalConfidence, ProposalStatus
from app.services.ai_config import get_chat_config
from app.services.claim_embedding import nearest_claims
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.prompts import (
    CLAIM_DEDUP_BATCH_SYSTEM,
    CLAIM_DEDUP_JUDGE_SYSTEM,
)
from app.services.intelligence.schemas import (
    ClaimDedupBatchResult,
    ClaimDedupJudgement,
    ClaimPairJudgement,
)

logger = logging.getLogger(__name__)

JUDGE_OPTIONS = {"temperature": 0.0, "num_predict": 200}
BATCH_SIZE = 50  # max candidate pairs per batched LLM call


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
    from app.services.ai_provider import chat_provider

    chat_provider.reload_from_db(db)
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
                proposed_at=naive_utc_now(),
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
            proposed_at=naive_utc_now(),
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


def _build_batch_prompt(pairs: list[tuple[int, str, int, str]]) -> str:
    """Format a list of (new_id, new_text, existing_id, existing_text) as a
    numbered pair list for CLAIM_DEDUP_BATCH_SYSTEM."""
    lines = [f"CANDIDATE CLAIM PAIRS ({len(pairs)} pairs):\n"]
    for i, (nid, ntxt, eid, etxt) in enumerate(pairs, 1):
        lines.append(f"Pair {i} [new=#{nid}, existing=#{eid}]:")
        lines.append(f"  #{nid}: {ntxt!r}")
        lines.append(f"  #{eid}: {etxt!r}\n")
    return "\n".join(lines)


def _judge_batch(
    pairs: list[tuple[int, str, int, str]],
    model: str,
    db: Session,
) -> list[ClaimPairJudgement]:
    """Send all candidate pairs to the LLM in one call. Returns only merge
    verdicts; unrecognised claim-ID pairs are dropped (LLM hallucination guard).
    """
    if not pairs:
        return []
    user_prompt = _build_batch_prompt(pairs)
    try:
        result: ClaimDedupBatchResult = call_json_ai(
            system_prompt=CLAIM_DEDUP_BATCH_SYSTEM,
            user_prompt=user_prompt,
            options={"temperature": 0.0, "num_predict": 1500},
            debug_label="dedup_batch",
            schema=ClaimDedupBatchResult,
            model=model or None,
            db=db,
            two_pass=False,
        )
        valid_pairs = {(nid, eid) for nid, _, eid, _ in pairs}
        return [
            m
            for m in result.merges
            if (m.new_claim_id, m.existing_claim_id) in valid_pairs
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedup batch call failed: %s", exc)
        return []


async def find_duplicates_for_case(
    case_id: str,
    db: Session,
    *,
    k: int = 3,
    skip_pair_if_proposal_exists: bool = True,
    progress_cb=None,
) -> dict[str, int]:
    """Wave 2C: retroactive duplicate-finder.

    Phase 1 — collect candidate pairs: for each claim, get its top-K
    embedding-nearest neighbours and deduplicate the resulting pair set.

    Phase 2 — batch judge: send all candidate pairs to the LLM in one call
    (chunked to BATCH_SIZE). Creates ClaimMergeProposal rows for each
    high/medium-confidence merge verdict returned.

    Idempotent: skips pairs that already have a proposal in either direction.
    Returns {scanned, proposals_created, judge_calls} for UI feedback.
    """
    from app.models.database import ClaimMergeProposal
    from app.repositories.claim import ClaimRepository

    cfg = get_chat_config(db)
    from app.services.ai_provider import chat_provider

    chat_provider.reload_from_db(db)
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

    # ------------------------------------------------------------------ #
    # Phase 1: collect all unique candidate pairs via embedding KNN       #
    # ------------------------------------------------------------------ #
    # Each entry: (new_id, new_text, existing_id, existing_text)
    # new_id is always the higher-id claim (deterministic directionality).
    candidate_pairs: list[tuple[int, str, int, str]] = []
    seen_pairs_this_run: set[tuple[int, int]] = set()

    for idx, claim in enumerate(claims, start=1):
        nearest = await nearest_claims(
            claim.claim_text,
            db,
            k=k,
            case_id=case_id,
            exclude_claim_id=claim.id,
        )
        for existing_id, _distance in nearest:
            new_id, existing_target_id = (
                (claim.id, existing_id)
                if claim.id > existing_id
                else (existing_id, claim.id)
            )
            pair = (new_id, existing_target_id)
            if pair in existing_pairs or pair in seen_pairs_this_run:
                continue
            seen_pairs_this_run.add(pair)

            new_claim_obj = db.get(Claim, new_id)
            existing_claim_obj = db.get(Claim, existing_target_id)
            if not new_claim_obj or not existing_claim_obj:
                continue
            candidate_pairs.append(
                (
                    new_id,
                    new_claim_obj.claim_text,
                    existing_target_id,
                    existing_claim_obj.claim_text,
                )
            )

        # Report Phase-1 progress every 10 claims (and at the end). Cheap
        # enough — the bottleneck is the embedding KNN above, not the cb.
        if progress_cb is not None and (idx % 10 == 0 or idx == len(claims)):
            try:
                progress_cb(processed=idx)
            except Exception as cb_err:
                logger.debug(f"dedup progress_cb failed (continuing): {cb_err}")

    if not candidate_pairs:
        return {"scanned": len(claims), "proposals_created": 0, "judge_calls": 0}

    # ------------------------------------------------------------------ #
    # Phase 2: batch judge — one LLM call per BATCH_SIZE chunk           #
    # ------------------------------------------------------------------ #
    confidence_map = {
        "high": ProposalConfidence.HIGH,
        "medium": ProposalConfidence.MEDIUM,
        "low": ProposalConfidence.LOW,
    }
    proposals_created = 0
    judge_calls = 0

    for batch_start in range(0, len(candidate_pairs), BATCH_SIZE):
        batch = candidate_pairs[batch_start : batch_start + BATCH_SIZE]
        judge_calls += 1
        judgements = _judge_batch(batch, cfg.summary_model, db)

        for j in judgements:
            if j.confidence not in ("high", "medium"):
                continue
            db.add(
                ClaimMergeProposal(
                    new_claim_id=j.new_claim_id,
                    existing_claim_id=j.existing_claim_id,
                    confidence=confidence_map.get(j.confidence, ProposalConfidence.LOW),
                    rationale=j.rationale,
                    status=ProposalStatus.PENDING,
                    proposed_at=naive_utc_now(),
                )
            )
            db.commit()
            proposals_created += 1
            existing_pairs.add((j.new_claim_id, j.existing_claim_id))
            logger.info(
                "retroactive merge proposal: claim %s ≈ %s (%s)",
                j.new_claim_id,
                j.existing_claim_id,
                j.confidence,
            )

    return {
        "scanned": len(claims),
        "proposals_created": proposals_created,
        "judge_calls": judge_calls,
    }
