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

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import DATA_DIR
from app.models.database import Document

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DATA_DIR / 'sanctuary.db'}"


def get_db_session():
    """Return a new SQLAlchemy session. Caller is responsible for closing it."""
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
    # Load sqlite-vec extension the same way alembic/env.py does
    try:
        import sqlite_vec  # noqa: F401
        from sqlalchemy import event as sa_event

        @sa_event.listens_for(engine, "connect")
        def load_sqlite_vec(dbapi_conn, _):
            dbapi_conn.enable_load_extension(True)
            sqlite_vec.load(dbapi_conn)
            dbapi_conn.enable_load_extension(False)
    except ImportError:
        pass

    Session = sessionmaker(bind=engine)
    return Session()


def run_migration(dry_run: bool = True) -> tuple[int, int]:
    """Move files and update DB records.

    Returns (moved_count, skipped_count).
    """
    moved = 0
    skipped = 0

    db = get_db_session()
    try:
        docs = (
            db.query(Document)
            .filter(
                Document.file_path.like("_TRIAGE/%"),
                Document.ingest_batch_id.isnot(None),
            )
            .all()
        )

        for doc in docs:
            fp = Path(doc.file_path)  # e.g. _TRIAGE/doc.pdf
            parts = fp.parts  # ('_TRIAGE', 'doc.pdf')

            # Skip docs already in a batch subfolder
            if len(parts) > 2:
                skipped += 1
                continue

            batch_subdir = f"ib-{doc.ingest_batch_id}"
            old_abs = DATA_DIR / fp
            new_rel = Path("_TRIAGE") / batch_subdir / fp.name
            new_abs = DATA_DIR / new_rel

            if not old_abs.exists():
                print(f"  WARN  file missing on disk, skipping: {old_abs}")
                skipped += 1
                continue

            print(f"  {'DRY ' if dry_run else ''}MOVE  {fp}  →  {new_rel}")

            if not dry_run:
                new_abs.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_abs), str(new_abs))
                doc.file_path = str(new_rel)
                moved += 1
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
