"""Wave 1 end-to-end verification: re-extract ib-0002 (court invoice) and
ib-0003 (court ruling), capture before/after, and print a diff.

Run with: .venv/bin/python scripts/verify_wave1_extraction.py
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Claim, ClaimEvidence, Document
from app.models.enums import ClaimEvidenceRole, ClaimStatus, OriginatorType

logging.basicConfig(level=logging.INFO, format="%(message)s")

DOC_IDS = [6, 7, 9]  # ib-0002 doc 6 (invoice), ib-0003 docs 7, 9
# doc 8 in ib-0003 is RELAY — extractor skips it intentionally


def snapshot_doc_state(db: Session, doc_id: int) -> dict:
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        return {"doc_id": doc_id, "missing": True}

    claims = (
        db.query(Claim)
        .filter(Claim.source_document_id == doc_id)
        .order_by(Claim.id)
        .all()
    )
    by_status: dict[ClaimStatus, int] = {}
    for c in claims:
        by_status[c.status] = by_status.get(c.status, 0) + 1

    asserts_rows = (
        db.query(ClaimEvidence)
        .filter(
            ClaimEvidence.document_id == doc_id,
            ClaimEvidence.role == ClaimEvidenceRole.ASSERTS,
        )
        .count()
    )
    return {
        "doc_id": doc_id,
        "title": doc.title,
        "originator": doc.originator_type.value if doc.originator_type else None,
        "tier": doc.significance_tier.value if doc.significance_tier else None,
        "doc_type": doc.document_type.value if doc.document_type else None,
        "claim_count": len(claims),
        "status_breakdown": {s.value: n for s, n in by_status.items()},
        "asserts_evidence_count": asserts_rows,
        "claim_texts": [(c.id, c.status.value, c.claim_text[:90]) for c in claims],
    }


def print_diff(before: dict, after: dict) -> None:
    print(f"\n{'=' * 80}")
    print(
        f"doc {before['doc_id']}  |  {before.get('title', '?')[:60]}  |  originator={before.get('originator')}"
    )
    print(f"{'=' * 80}")
    if "missing" in before or "missing" in after:
        print("  MISSING DOC")
        return

    print(f"  claims: {before['claim_count']} → {after['claim_count']}")
    print(
        f"  asserts evidence rows: {before['asserts_evidence_count']} → {after['asserts_evidence_count']}"
    )
    print(f"  status breakdown before: {before['status_breakdown']}")
    print(f"  status breakdown after:  {after['status_breakdown']}")

    before_ids = {c[0] for c in before["claim_texts"]}
    after_ids = {c[0] for c in after["claim_texts"]}
    deleted = before_ids - after_ids
    added = after_ids - before_ids
    surviving = before_ids & after_ids

    print(
        f"\n  deleted: {len(deleted)}  surviving: {len(surviving)}  added: {len(added)}"
    )

    if added:
        print("\n  NEW claims:")
        for cid, status, text in after["claim_texts"]:
            if cid in added:
                marker = "[!]" if status == "established" else "[ ]"
                print(f"    {marker} #{cid:>4}  {status:<11}  {text}")

    if deleted:
        print("\n  DELETED claims (cleared on retry):")
        for cid, status, text in before["claim_texts"]:
            if cid in deleted:
                print(f"    --- #{cid:>4}  {status:<11}  {text}")


def main() -> int:
    print("Wave 1 verification — re-extracting ib-0002 (doc 6) and ib-0003 (docs 7, 9)")
    print("doc 8 is RELAY and is intentionally skipped by the extractor.\n")

    db = SessionLocal()
    try:
        before = {doc_id: snapshot_doc_state(db, doc_id) for doc_id in DOC_IDS}
        for doc_id in DOC_IDS:
            print(f"  before: doc {doc_id}: {before[doc_id]['claim_count']} claims")
    finally:
        db.close()

    print("\nRunning extraction (will hit LiteLLM at the active instance)...\n")
    from app.services.intelligence.claim_extractor import extract

    for doc_id in DOC_IDS:
        try:
            skipped = extract(doc_id)
            if skipped:
                print(f"  doc {doc_id}: SKIPPED — {skipped}")
            else:
                print(f"  doc {doc_id}: re-extracted")
        except Exception as exc:  # noqa: BLE001
            print(f"  doc {doc_id}: FAILED — {exc}")
            import traceback

            traceback.print_exc()
            return 1

    db = SessionLocal()
    try:
        after = {doc_id: snapshot_doc_state(db, doc_id) for doc_id in DOC_IDS}
    finally:
        db.close()

    for doc_id in DOC_IDS:
        print_diff(before[doc_id], after[doc_id])

    print(f"\n{'=' * 80}")
    print("Wave 1 invariants to spot-check:")
    print(f"{'=' * 80}")
    for doc_id in DOC_IDS:
        a = after[doc_id]
        if "missing" in a:
            continue
        if a["originator"] == OriginatorType.COURT.value:
            est = a["status_breakdown"].get("established", 0)
            tot = a["claim_count"]
            ok = "✓" if est == tot and tot > 0 else "✗"
            print(
                f"  {ok} doc {doc_id} (COURT): {est}/{tot} claims arrived ESTABLISHED"
            )
        else:
            ass = a["status_breakdown"].get("asserted", 0)
            tot = a["claim_count"]
            print(
                f"  · doc {doc_id} ({a['originator']}): {ass}/{tot} ASSERTED (non-court → default lifecycle)"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
