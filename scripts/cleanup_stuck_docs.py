import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.append(os.getcwd())

from app.config import SessionLocal
from app.models.database import Document
from app.models.enums import PipelineState


def cleanup():
    """Identifies documents stuck in 'running' state for > 60 mins and resets them."""
    db = SessionLocal()
    # Find docs stuck in running for > 60 mins
    timeout_limit = datetime.now(UTC) - timedelta(minutes=60)

    stuck_docs = (
        db.query(Document)
        .filter(
            (Document.pipeline_state == PipelineState.RUNNING)
            | (Document.pipeline_state == PipelineState.PARTIAL)
        )
        .all()
    )

    count = 0
    for doc in stuck_docs:
        stages = doc.pipeline_stages or {}
        modified = False

        for stage_name, info in stages.items():
            if info.get("status") == "running":
                started_at_str = info.get("started_at")
                if started_at_str:
                    try:
                        # Handle potential timezone offsets in ISO string
                        dt = datetime.fromisoformat(
                            started_at_str.replace("Z", "+00:00")
                        )
                        if dt < timeout_limit:
                            print(
                                f"Doc {doc.id} stage {stage_name} stuck since {started_at_str}. Resetting."
                            )
                            info["status"] = "pending"
                            info["error"] = "stalled (auto-cleaned)"
                            modified = True
                    except ValueError:
                        continue

        if modified:
            doc.pipeline_stages = stages
            # If the overall state was 'running', set it back to 'partial'
            # so the UI allows a fresh retry.
            if doc.pipeline_state == PipelineState.RUNNING:
                doc.pipeline_state = PipelineState.PARTIAL
            count += 1

    db.commit()
    if count > 0:
        print(f"Cleaned up {count} stuck documents.")
    db.close()


if __name__ == "__main__":
    cleanup()
