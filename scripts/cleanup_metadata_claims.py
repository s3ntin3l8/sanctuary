"""One-shot cleanup of metadata/letterhead claims that should never have been
extracted. Identifies REFUTED claims whose text matches known metadata
patterns and deletes them (along with their cascade-delete ClaimEvidence rows).

Targets only REFUTED claims because:
- ESTABLISHED claims (court findings) are authoritative and never deleted
- ASSERTED claims auto-clear on retry, so any noise there will be regenerated
  cleanly under the V3 prompt
- CONTESTED claims have user/AI signal worth preserving
- REFUTED is where stale noise accumulates: pre-V3 the AI extracted bogus
  metadata claims, then later doc extractions auto-flipped them to REFUTED
  via wrong evidence_links (see commit ca21314 commit message and the
  brainstorming session that preceded it).

Patterns deleted are conservative — only obvious metadata. Run dry-first.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/cleanup_metadata_claims.py        # dry run
  PYTHONPATH=. .venv/bin/python scripts/cleanup_metadata_claims.py --apply
"""

from __future__ import annotations

import re
import sys

from app.config import SessionLocal
from app.models.database import Claim, ClaimEvidence
from app.models.enums import ClaimEvidenceRole, ClaimStatus

# Patterns that match pure metadata/letterhead claims.
# Each pattern is anchored on the typical claim shape "the X is Y"
# where X is a document-property and Y is its value.
PATTERNS = [
    # Date metadata
    re.compile(
        r"\bthe (document|letter|notification|correspondence) (is|was) dated\b", re.I
    ),
    re.compile(
        r"\bthe (document|letter|notification|correspondence) (is|was) issued\b", re.I
    ),
    re.compile(r"\bthe (document|letter) states the (issue )?date\b", re.I),
    re.compile(r"\bthe issue date (of (this|the) document )?is\b", re.I),
    # Recipient/addressee metadata
    re.compile(
        r"\bthe (document|letter|notification|correspondence) is addressed to\b", re.I
    ),
    re.compile(
        r"\bthe recipient (of (this|the) (document|letter|correspondence|notification) )?is\b",
        re.I,
    ),
    re.compile(
        r"\bthe addressee (of (this|the) (document|letter|correspondence|notification) )?is\b",
        re.I,
    ),
    # Sender metadata
    re.compile(
        r"\bthe sender (of (this|the) (document|letter|correspondence|notification) )?is\b",
        re.I,
    ),
    re.compile(
        r"\bthe (document|letter|notification|correspondence) (is from|originates from|was sent by)\b",
        re.I,
    ),
    # Reference / case number metadata
    re.compile(
        r"\bthe (internal reference|case|file) number (for|of) (this|the) (correspondence|document|letter)\b",
        re.I,
    ),
    re.compile(
        r"\bthe (AZ|Aktenzeichen) (for|of) (this|the) (correspondence|document|letter)\b",
        re.I,
    ),
]


def matches_metadata_pattern(text: str) -> str | None:
    for pat in PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


def main(apply: bool) -> int:
    db = SessionLocal()
    try:
        refuted = (
            db.query(Claim)
            .filter(Claim.status == ClaimStatus.REFUTED)
            .order_by(Claim.id)
            .all()
        )
        # Originating document per claim — the ASSERTS evidence row is the
        # canonical "originated by" link (Claim.source_document_id no longer
        # exists; see ClaimRepository.claims_asserted_by_document).
        source_doc_by_claim: dict[int, int] = {
            row[0]: row[1]
            for row in db.query(ClaimEvidence.claim_id, ClaimEvidence.document_id)
            .filter(
                ClaimEvidence.claim_id.in_([c.id for c in refuted]),
                ClaimEvidence.role == ClaimEvidenceRole.ASSERTS,
            )
            .all()
        }

        candidates: list[tuple[Claim, str]] = []
        kept: list[Claim] = []
        for c in refuted:
            pat = matches_metadata_pattern(c.claim_text)
            if pat:
                candidates.append((c, pat))
            else:
                kept.append(c)

        print(
            f"Surveyed {len(refuted)} REFUTED claims:  "
            f"{len(candidates)} match metadata patterns, {len(kept)} preserved."
        )
        print()
        print(
            f"{'=' * 80}\nWILL BE DELETED ({'apply' if apply else 'DRY RUN'})\n{'=' * 80}"
        )
        for c, pat in candidates:
            print(
                f"  #{c.id:>4}  doc={source_doc_by_claim.get(c.id, '?'):>3}  type={c.claim_type.value:<11}  {c.claim_text[:90]}"
            )
            print(f"        matched: {pat}")

        print()
        print(f"{'=' * 80}\nKEPT (preserved as legitimate REFUTED signal)\n{'=' * 80}")
        for c in kept:
            print(
                f"  #{c.id:>4}  doc={source_doc_by_claim.get(c.id, '?'):>3}  type={c.claim_type.value:<11}  {c.claim_text[:90]}"
            )

        if not apply:
            print()
            print("Dry run complete. Re-run with --apply to delete the matched claims.")
            return 0

        if not candidates:
            print("Nothing to delete.")
            return 0

        for c, _ in candidates:
            db.delete(c)
        db.commit()
        print(
            f"\nDeleted {len(candidates)} metadata claim(s). ClaimEvidence rows cascade-deleted."
        )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    sys.exit(main(apply))
