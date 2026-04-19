"""Tests for atomic batch claim_for_analysis."""

from datetime import datetime

import pytest

from app.models.database import Document, IngestBatch
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    IngestStatus,
    OriginatorType,
)
from app.repositories.ingest_batch import IngestBatchRepository


@pytest.fixture
def ready_batch(db_session, sample_case):
    """A batch whose docs are all COMPLETED."""
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        created_at=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="Doc 1",
        content="Content",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        ingest_status=IngestStatus.COMPLETED,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(batch)
    return batch


@pytest.fixture
def pending_batch(db_session, sample_case):
    """A batch with at least one PENDING doc."""
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        created_at=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="Doc 1",
        content="Content",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        ingest_status=IngestStatus.PENDING,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(batch)
    return batch


@pytest.mark.unit
def test_claim_returns_true_for_ready_batch(db_session, ready_batch):
    repo = IngestBatchRepository(db_session)
    result = repo.claim_for_analysis(ready_batch.id)
    assert result is True


@pytest.mark.unit
def test_claim_returns_false_second_call(db_session, ready_batch):
    """Second call must return False — batch already claimed."""
    repo = IngestBatchRepository(db_session)
    first = repo.claim_for_analysis(ready_batch.id)
    second = repo.claim_for_analysis(ready_batch.id)
    assert first is True
    assert second is False


@pytest.mark.unit
def test_claim_returns_false_for_pending_docs(db_session, pending_batch):
    """Batch with unfinished docs must not be claimed."""
    repo = IngestBatchRepository(db_session)
    result = repo.claim_for_analysis(pending_batch.id)
    assert result is False
