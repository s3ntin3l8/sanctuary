"""One-shot migration: organise _TRIAGE flat files into ib-{batch_id} subdirectories.

Usage:
    python scripts/migrate_triage_to_batch_dirs.py          # live run
    python scripts/migrate_triage_to_batch_dirs.py --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Allow running as a top-level script
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import DATA_DIR, SessionLocal
from app.models.database import Document


def get_db_session():
    """Return a new SQLAlchemy session. Caller is responsible for closing it."""
    return SessionLocal()


def run_migration(dry_run: bool = True) -> tuple[int, int]:
    """Move files and update DB records.

    Handles both absolute paths (e.g. /…/data/_TRIAGE/doc.pdf) and relative
    paths (e.g. _TRIAGE/doc.pdf) — batch_orchestrator stores absolute paths.
    Migrated documents retain absolute-path format (consistent with batch_orchestrator);
    the move_document_file_on_assignment event listener normalises to relative on confirmation.

    Returns (moved_count, skipped_count).
    """
    moved = 0
    skipped = 0

    db = get_db_session()
    try:
        # Use .contains() to match both absolute paths (e.g. /…/data/_TRIAGE/doc.pdf)
        # and relative paths (e.g. _TRIAGE/doc.pdf) — batch_orchestrator stores absolute.
        # The Python-level parts[0] check below provides a second guard against false matches.
        docs = (
            db.query(Document)
            .filter(
                Document.file_path.contains("_TRIAGE"),
                Document.ingest_batch_id.isnot(None),
            )
            .all()
        )

        for doc in docs:
            fp = Path(doc.file_path)

            # Normalize to relative path from DATA_DIR (handles absolute paths)
            if fp.is_absolute():
                try:
                    rel = fp.relative_to(DATA_DIR)
                except ValueError:
                    print(f"  WARN  path not under DATA_DIR, skipping: {fp}")
                    skipped += 1
                    continue
            else:
                rel = fp

            parts = rel.parts  # e.g. ('_TRIAGE', 'doc.pdf')

            # Skip docs not in _TRIAGE root
            if not parts or parts[0] != "_TRIAGE":
                skipped += 1
                continue

            # Skip docs already in a batch subfolder
            if len(parts) > 2:
                print(f"  SKIP  already in subfolder: {rel}")
                skipped += 1
                continue

            batch_subdir = f"ib-{doc.ingest_batch_id}"
            old_abs = DATA_DIR / rel
            new_rel = Path("_TRIAGE") / batch_subdir / rel.name
            new_abs = DATA_DIR / new_rel

            if not old_abs.exists():
                print(f"  WARN  file missing on disk, skipping: {old_abs}")
                skipped += 1
                continue

            print(f"  {'DRY ' if dry_run else ''}MOVE  {rel}  →  {new_rel}")

            if not dry_run:
                new_abs.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(old_abs), str(new_abs))
                    doc.file_path = str(new_abs)
                    moved += 1
                except OSError as e:
                    print(f"  ERROR  could not move {old_abs}: {e}")
                    skipped += 1
            else:
                moved += 1  # count as "would move"

        if not dry_run:
            db.commit()
    finally:
        db.close()

    return moved, skipped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate _TRIAGE files to ib-{id} subfolders"
    )
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args()

    moved, skipped = run_migration(dry_run=args.dry_run)
    label = "Would move" if args.dry_run else "Moved"
    print(f"\n{label}: {moved}  |  Skipped: {skipped}")
