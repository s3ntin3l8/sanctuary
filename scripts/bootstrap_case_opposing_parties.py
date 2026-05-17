#!/usr/bin/env python3
"""One-shot: populate Case.opposing_parties from existing case.parties JSON.

For every case with a non-empty case.parties list, derive the opposing parties
list (names with role == "opposing") and write it to case.opposing_parties.
Idempotent — only fills when opposing_parties is currently null/empty.

Run once after the add_case_opposing_parties migration:
  python scripts/bootstrap_case_opposing_parties.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import SessionLocal
from app.models.database import Case


def main() -> None:
    db = SessionLocal()
    try:
        cases = db.query(Case).filter(Case.id != "_TRIAGE").all()
        updated = 0
        skipped = 0
        for case in cases:
            if case.opposing_parties:
                skipped += 1
                continue
            if not case.parties:
                skipped += 1
                continue
            opposing = [
                p["name"]
                for p in case.parties
                if isinstance(p, dict) and p.get("role") == "opposing" and p.get("name")
            ]
            if opposing:
                case.opposing_parties = opposing
                updated += 1
                print(f"  {case.id}: {opposing}")
            else:
                skipped += 1
        db.commit()
        print(f"\nDone: {updated} cases bootstrapped, {skipped} skipped.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
