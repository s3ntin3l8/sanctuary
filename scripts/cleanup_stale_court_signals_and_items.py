"""One-shot DB cleanup: erase Streitwert signals and action items rooted in
non-court source documents.

This script catches up the live DB with the Round 2 gates:
- `app/services/cost_service.py` — Streitwert is only authoritative when set
  BY a court IN a direct court document.
- `app/services/intelligence/document_enricher.py` — Action items must come
  from direct court documents (originator=COURT AND NOT court_relay).

Run once after the Round 2 code lands, then delete this script.

Usage:
    .venv/bin/python scripts/cleanup_stale_court_signals_and_items.py [--dry-run]
"""

import argparse
import os
import sys

sys.path.append(os.getcwd())

from sqlalchemy import text  # noqa: E402

from app.config import SessionLocal  # noqa: E402


def _qualifying_filter() -> str:
    """SQL predicate identifying source documents that are NOT a direct court
    source. Matches the Python condition `not (originator_type == COURT and
    not court_relay)`. Documents missing originator_type (NULL) are also
    treated as non-court — they never qualified to authoritatively set
    Streitwert or action items either.
    """
    # SAEnum stores the enum NAME (uppercase), not the value — see e.g.
    # `SELECT DISTINCT originator_type FROM documents` returning COURT/OPPOSING/...
    return (
        "source_document_id IN ("
        "SELECT id FROM documents "
        "WHERE originator_type IS NULL "
        "   OR originator_type != 'COURT' "
        "   OR court_relay = 1"
        ")"
    )


def _report_streitwert_to_delete(db) -> list[tuple]:
    rows = db.execute(
        text(
            "SELECT cs.id, cs.source_document_id, cs.amount, "
            "       substr(cs.description, 1, 60) as description, "
            "       d.originator_type, d.court_relay, substr(d.title, 1, 60) as doc_title "
            "FROM cost_signals cs "
            "LEFT JOIN documents d ON d.id = cs.source_document_id "
            f"WHERE cs.signal_type = 'streitwert' AND cs.{_qualifying_filter()}"
        )
    ).fetchall()
    return rows


def _report_action_items_to_delete(db) -> list[tuple]:
    rows = db.execute(
        text(
            "SELECT ai.id, ai.source_document_id, ai.due_date, ai.action_type, "
            "       substr(ai.title, 1, 60) as title, "
            "       d.originator_type, d.court_relay "
            "FROM action_items ai "
            "LEFT JOIN documents d ON d.id = ai.source_document_id "
            f"WHERE ai.superseded = 0 AND ai.{_qualifying_filter()}"
        )
    ).fetchall()
    return rows


def cleanup(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        streitwert_rows = _report_streitwert_to_delete(db)
        action_item_rows = _report_action_items_to_delete(db)

        print(f"Found {len(streitwert_rows)} stale Streitwert signal(s) to erase:")
        for row in streitwert_rows:
            print(
                f"  signal#{row[0]}  doc#{row[1]}  amount={row[2]}  "
                f"originator={row[4]}  court_relay={row[5]}"
            )
            print(f"      description: {row[3]!r}")
            print(f"      doc title:   {row[6]!r}")

        print(
            f"\nFound {len(action_item_rows)} stale action item(s) to erase "
            f"(non-superseded only):"
        )
        for row in action_item_rows:
            print(
                f"  item#{row[0]}  doc#{row[1]}  due={row[2]}  type={row[3]}  "
                f"originator={row[5]}  court_relay={row[6]}"
            )
            print(f"      title: {row[4]!r}")

        if dry_run:
            print("\n[dry-run] no rows deleted.")
            return

        if not streitwert_rows and not action_item_rows:
            print("\nNothing to delete.")
            return

        db.execute(
            text(
                "DELETE FROM cost_signals "
                f"WHERE signal_type = 'streitwert' AND {_qualifying_filter()}"
            )
        )
        db.execute(
            text(
                "DELETE FROM action_items "
                f"WHERE superseded = 0 AND {_qualifying_filter()}"
            )
        )
        db.commit()
        print(
            f"\nDeleted {len(streitwert_rows)} Streitwert signal(s) and "
            f"{len(action_item_rows)} action item(s)."
        )
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report without deleting"
    )
    args = parser.parse_args()
    cleanup(dry_run=args.dry_run)
    sys.exit(0)
