"""Wave 2B one-shot: embed every existing claim into claim_vectors so the
dedup judge can find neighbors. New claims embed automatically via the
extractor's pipeline; this script bridges the gap for claims that pre-date
Wave 2B.

Run with: PYTHONPATH=. .venv/bin/python scripts/backfill_claim_embeddings.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.config import SessionLocal
from app.models.database import Claim
from app.services.claim_embedding import upsert_claim_embedding


async def _run() -> int:
    db = SessionLocal()
    try:
        already = {
            r[0]
            for r in db.execute(text("SELECT claim_id FROM claim_vectors")).fetchall()
        }
        all_claims = db.query(Claim).order_by(Claim.id).all()
        todo = [c for c in all_claims if c.id not in already]
        print(
            f"Total claims: {len(all_claims)};  already embedded: {len(already)};  to embed: {len(todo)}"
        )
        if not todo:
            return 0

        ok = 0
        failed = 0
        for c in todo:
            success = await upsert_claim_embedding(c.id, db)
            if success:
                ok += 1
                if ok % 25 == 0:
                    print(f"  embedded {ok} / {len(todo)}…")
            else:
                failed += 1
        print(f"Done. Embedded {ok}; failed {failed}.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
