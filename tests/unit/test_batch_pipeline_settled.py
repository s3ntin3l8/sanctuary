"""batch_pipeline_settled: has every document of a batch finished its pipeline?"""

import pytest

from app.models.database import Document, DocumentPipelineStage, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.services.pipeline_status import batch_pipeline_settled

pytestmark = pytest.mark.unit


def _batch(db_session, *docs_stages: list[str], status=None) -> int:
    """A batch with one document per entry; each entry lists that doc's stage statuses."""
    batch = IngestBatch(source_type=IngestBatchSourceType.EMAIL, subject="s")
    if status is not None:
        batch.status = status
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


def test_a_batch_waiting_for_the_users_slice_review_counts_as_settled(db_session):
    """No amount of waiting finishes it — the user has to confirm the slices —
    so a history import must move on rather than stall for the full timeout."""
    batch_id = _batch(db_session, [], status=IngestBatchStatus.AWAITING_SLICING)
    assert batch_pipeline_settled(db_session, batch_id) is True
    # ...whereas the same shape in any other status is still unsettled.
    other = _batch(db_session, [], status=IngestBatchStatus.PROCESSING)
    assert batch_pipeline_settled(db_session, other) is False
