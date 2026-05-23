"""One-shot data cleanup: strip court entities from `case.opposing_parties`
and recompute `case.parties` with the Fix 8a COURT-lock logic.

Round 3 of the ib-0039 fix chain. Courts had been auto-bootstrapped into
`case.opposing_parties` by `_compute_parties`' old "discount COURT"
heuristic. The structural fix prevents future pollution; this script cleans
the live DB to break the feedback loop on existing cases.

Two passes, both with dry-run support:

  1. Strip court names from `case.opposing_parties` for every case.
  2. Recompute `case.parties` via `_compute_parties` so the case graph and
     AI brief inputs reflect the corrected roles immediately (without
     waiting for the next case-brief regeneration).

Usage:
    .venv/bin/python scripts/cleanup_courts_from_opposing_parties.py [--dry-run]
"""

import argparse
import os
import sys

sys.path.append(os.getcwd())

from sqlalchemy.orm import defer  # noqa: E402

from app.config import SessionLocal  # noqa: E402
from app.models.database import Case, Document  # noqa: E402
from app.services.intelligence._court_identity import is_court_name  # noqa: E402
from app.services.intelligence.case_brief_generator import (
    _compute_parties,  # noqa: E402
)
from app.services.user_settings_service import get_party_identity  # noqa: E402


def _strip_courts_from_opposing(case: Case) -> list[str]:
    """Return the list of court names that need removal from this case's
    opposing_parties (empty list when no change is needed)."""
    if not case.opposing_parties:
        return []
    return [name for name in case.opposing_parties if is_court_name(name)]


def _recompute_court_entries_diff(
    case: Case, db
) -> tuple[list[dict], list[dict]] | None:
    """Recompute case.parties, but surgically apply only court-name entries.

    Non-court entries keep their existing role and document_count from
    `case.parties` — this script's mandate is "courts wrongly flagged as
    parties", not "refresh everything". Returns (old_parties, patched_parties)
    when at least one court-name entry's role changes, else None.
    """
    docs = (
        db.query(Document)
        .filter(Document.case_id == case.id)
        .options(defer(Document.content))
        .all()
    )
    settings = get_party_identity(db)
    own_self = (settings.get("own_self") or "").strip()
    own_parties = settings.get("own_parties") or []
    new_parties = _compute_parties(
        docs,
        own_self=own_self,
        own_parties=own_parties,
        opposing_parties=case.opposing_parties or [],
    )
    old_parties = case.parties or []
    new_by_name = {p["name"]: p for p in new_parties}

    patched: list[dict] = []
    changed = False
    for old_p in old_parties:
        name = old_p["name"]
        if is_court_name(name):
            # Court name — pull the fresh entry (locks COURT role under Fix 8a).
            new_p = new_by_name.get(name)
            if new_p:
                if new_p.get("role") != old_p.get("role"):
                    changed = True
                patched.append(new_p)
            else:
                # Court no longer in docs at all — drop it from parties.
                changed = True
        else:
            patched.append(old_p)

    # Append any new court entries that weren't in the old list (rare, but
    # possible when a freshly-ingested case is being cleaned).
    old_names = {p["name"] for p in old_parties}
    for new_p in new_parties:
        if new_p["name"] not in old_names and is_court_name(new_p["name"]):
            patched.append(new_p)
            changed = True

    if not changed:
        return None
    return (old_parties, patched)


def cleanup(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        cases = db.query(Case).all()

        # Pass 1: opposing_parties
        opposing_changes: list[tuple[str, list[str], list[str]]] = []
        for case in cases:
            removed = _strip_courts_from_opposing(case)
            if removed:
                kept = [n for n in case.opposing_parties if n not in removed]
                opposing_changes.append((case.id, removed, kept))

        # Pass 2: case.parties — surgical court-name updates only.
        parties_changes: list[tuple[str, list[dict], list[dict]]] = []
        for case in cases:
            diff = _recompute_court_entries_diff(case, db)
            if diff is not None:
                old_list, new_list = diff
                parties_changes.append((case.id, old_list, new_list))

        # Report
        print(
            f"Pass 1 — case.opposing_parties: {len(opposing_changes)} case(s) "
            f"to update."
        )
        for case_id, removed, kept in opposing_changes:
            print(f"  case={case_id}")
            print(f"    remove: {removed}")
            print(f"    keep:   {kept}")

        print(
            f"\nPass 2 — case.parties (graph source): {len(parties_changes)} "
            f"case(s) where a court's role flips."
        )
        for case_id, old_list, new_list in parties_changes:
            print(f"  case={case_id}")
            old_by_name = {p["name"]: p for p in old_list}
            for new_p in new_list:
                old_p = old_by_name.get(new_p["name"])
                if old_p and old_p.get("role") != new_p.get("role"):
                    print(
                        f"    {new_p['name']!r}: "
                        f"role {old_p.get('role')} -> {new_p.get('role')} "
                        f"({new_p.get('document_count')} docs)"
                    )

        if dry_run:
            print("\n[dry-run] no changes written.")
            return

        if not opposing_changes and not parties_changes:
            print("\nNothing to update.")
            return

        for case_id, _removed, kept in opposing_changes:
            case = db.query(Case).filter(Case.id == case_id).first()
            if case:
                case.opposing_parties = kept
        for case_id, _old, new_list in parties_changes:
            case = db.query(Case).filter(Case.id == case_id).first()
            if case:
                case.parties = new_list

        db.commit()
        print(
            f"\nUpdated {len(opposing_changes)} opposing_parties row(s) "
            f"and {len(parties_changes)} parties row(s)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()
    cleanup(dry_run=args.dry_run)
    sys.exit(0)
