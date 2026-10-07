"""batch_pipeline_settled: has every document of a batch finished its pipeline?"""

import pytest

from app.models.database import Document, DocumentPipelineStage, IngestBatch
from app.models.enums import IngestBatchSourceType
from app.services.pipeline_status import batch_pipeline_settled

pytestmark = pytest.mark.unit


def _batch(db_session, *docs_stages: list[str]) -> int:
    """A batch with one document per entry; each entry lists that doc's stage statuses."""
    batch = IngestBatch(source_type=IngestBatchSourceType.EMAIL, subject="s")
    db_session.add(batch)
    db_session.flush()
    for index, statuses in enumerate(docs_stages):
        doc = Document(title=f"d{index}", ingest_batch_id=batch.id, case_id="_TRIAGE")
        db_session.add(doc)
        db_session.flush()
        for n, status in enumerate(statuses):
            db_session.add(
                DocumentPipelineStage(
                    document_id=doc.id, stage=f"stage{n}", status=status
                )
            )
    db_session.commit()
    return batch.id


@pytest.mark.parametrize(
    ("docs", "settled"),
    [
        ([], True),  # nothing to wait for
        ([[]], False),  # dispatched, no stage initialised yet
        ([["running"]], False),
        ([["completed", "retrying"]], False),
        ([["completed", "pending"]], False),  # work still ahead
        ([["completed", "skipped"]], True),
        ([["failed", "completed"]], True),  # failed is terminal
        ([["failed", "pending"]], True),  # downstream of a failure never runs
        ([["dismissed"]], True),  # the user closed it out
        ([["dismissed", "completed"]], True),
        ([["completed"], ["running"]], False),  # one busy document holds the batch
        ([["completed"], ["completed"]], True),
    ],
)
def test_settled(db_session, docs, settled):
    assert batch_pipeline_settled(db_session, _batch(db_session, *docs)) is settled
