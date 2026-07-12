"""Wave 2C smoke run: find duplicates across every case in the corpus and
print the resulting proposals so we can eyeball whether the judge is
catching real duplicates without false positives.

Run with: PYTHONPATH=. .venv/bin/python scripts/run_find_duplicates_all_cases.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.config import SessionLocal
from app.models.database import Case, Claim, ClaimMergeProposal
from app.models.enums import ProposalStatus
from app.services.intelligence.claim_dedup_judge import find_duplicates_for_case


async def main() -> int:
    db = SessionLocal()
    try:
        cases = db.query(Case).filter(Case.id != "_TRIAGE").order_by(Case.id).all()
        cases_with_claims = []
        for c in cases:
            n = db.execute(
                text(
                    "SELECT COUNT(DISTINCT c.id) "
                    "FROM claims c "
                    "JOIN claim_evidence ce ON c.id = ce.claim_id "
                    "JOIN documents d ON d.id = ce.document_id "
                    "WHERE d.case_id = :cid"
                ),
                {"cid": c.id},
            ).scalar()
            if n:
                cases_with_claims.append((c.id, n))

        print(f"Cases with claims: {len(cases_with_claims)}")
        for cid, n in cases_with_claims:
            print(f"  {cid}: {n} claims")
        print()

        for cid, n in cases_with_claims:
            print(f"\n{'=' * 80}")
            print(f"Running find-duplicates for {cid} ({n} claims)…")
            print(f"{'=' * 80}")
            stats = await find_duplicates_for_case(cid, db, k=3)
            db.commit()
            print(
                f"  scanned={stats['scanned']}  judge_calls={stats['judge_calls']}  "
                f"proposals_created={stats['proposals_created']}"
            )

        # Print the full set of pending merge proposals.
        print(f"\n{'=' * 80}")
        print("ALL PENDING MERGE PROPOSALS")
        print(f"{'=' * 80}")
        proposals = (
            db.query(ClaimMergeProposal)
            .filter(ClaimMergeProposal.status == ProposalStatus.PENDING)
            .order_by(ClaimMergeProposal.proposed_at.desc())
            .all()
        )
        if not proposals:
            print("  (none)")
            return 0

        claim_ids = {p.new_claim_id for p in proposals} | {
            p.existing_claim_id for p in proposals
        }
        claims_by_id = {
            c.id: c for c in db.query(Claim).filter(Claim.id.in_(claim_ids)).all()
        }
        for p in proposals:
            new = claims_by_id.get(p.new_claim_id)
            existing = claims_by_id.get(p.existing_claim_id)
            print(f"\n  proposal #{p.id}  [{p.confidence.value}]")
            print(
                f"    new      #{p.new_claim_id}: {(new.claim_text if new else '?')[:120]}"
            )
            print(
                f"    existing #{p.existing_claim_id}: {(existing.claim_text if existing else '?')[:120]}"
            )
            if p.rationale:
                print(f"    rationale: {p.rationale[:160]}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
