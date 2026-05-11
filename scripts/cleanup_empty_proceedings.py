import os
import sys

# Ensure the app directory is in the path
sys.path.append(os.getcwd())

from app.config import SessionLocal
from app.models.database import ActionItem, Document, IngestBatch, LegalCost, Proceeding


def cleanup():
    """Removes proceedings that have no documents, batches, actions, or costs."""
    print("Starting cleanup of empty proceedings...")
    db = SessionLocal()
    try:
        proceedings = db.query(Proceeding).all()

        deleted_count = 0
        for p in proceedings:
            doc_count = (
                db.query(Document).filter(Document.proceeding_id == p.id).count()
            )
            batch_count = (
                db.query(IngestBatch).filter(IngestBatch.proceeding_id == p.id).count()
            )
            action_count = (
                db.query(ActionItem).filter(ActionItem.proceeding_id == p.id).count()
            )
            cost_count = (
                db.query(LegalCost).filter(LegalCost.proceeding_id == p.id).count()
            )

            if (
                doc_count == 0
                and batch_count == 0
                and action_count == 0
                and cost_count == 0
            ):
                print(
                    f"Deleting empty proceeding {p.id}: {p.court_name} ({p.az_court or 'No AZ'})"
                )
                db.delete(p)
                deleted_count += 1

        db.commit()
        print(f"Cleanup complete. Deleted {deleted_count} proceedings.")
    except Exception as e:
        db.rollback()
        print(f"Error during cleanup: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    cleanup()
