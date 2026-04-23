import sys
from pathlib import Path

from sqlalchemy.orm import Session

# Add app to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.config import SessionLocal
from app.models.database import Document
from app.services.ai_summary import _summarize_document_sync


def trigger_all_missing_summaries():
    db: Session = SessionLocal()
    try:
        # Find documents with no summary or failed summary
        docs = db.query(Document).filter(Document.ai_summary.is_(None)).all()

        print(f"[*] Found {len(docs)} documents without valid AI summaries.")

        for i, doc in enumerate(docs):
            print(
                f"    [{i + 1}/{len(docs)}] Summarizing: {doc.title} (ID: {doc.id})...",
                end="",
                flush=True,
            )
            try:
                _summarize_document_sync(doc.id, db)
                print(" [✓] Done")
            except Exception as e:
                print(f" [✗] Failed: {e}")

        print("\n[✓] Bulk processing complete.")

    finally:
        db.close()


if __name__ == "__main__":
    print("=== Sanctuary Bulk AI Summary Tool ===\n")
    trigger_all_missing_summaries()
