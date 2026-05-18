from datetime import UTC, datetime

import pytest

from app.models.database import ActionItem, Document, IngestBatch
from app.models.enums import (
    ActionItemStatus,
    DocumentStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
)
from app.services.triage_bundles import get_triage_bundles
from app.services.triage_dismissal import dismiss_bundle


@pytest.mark.unit
def test_dismiss_batch_updates_statuses(db_session):
    batch = IngestBatch(source_type=IngestBatchSourceType.EMAIL, subject="Test Batch")
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)

    doc1 = Document(title="Doc 1", ingest_batch_id=batch.id, case_id="_TRIAGE")
    doc2 = Document(title="Doc 2", ingest_batch_id=batch.id, case_id="_TRIAGE")
    db_session.add_all([doc1, doc2])
    db_session.commit()
    db_session.refresh(doc1)
    db_session.refresh(doc2)

    # Add action items
    ai1 = ActionItem(
        case_id="_TRIAGE",
        source_document_id=doc1.id,
        title="Action 1",
        due_date=datetime.now(UTC),
    )
    ai2 = ActionItem(
        case_id="_TRIAGE",
        source_document_id=doc2.id,
        title="Action 2",
        due_date=datetime.now(UTC),
    )
    db_session.add_all([ai1, ai2])
    db_session.commit()

    success = dismiss_bundle(db_session, batch_id=batch.id)
    assert success is True

    db_session.refresh(batch)
    db_session.refresh(doc1)
    db_session.refresh(doc2)
    db_session.refresh(ai1)
    db_session.refresh(ai2)

    assert batch.status == IngestBatchStatus.DISMISSED
    assert doc1.status == DocumentStatus.DISMISSED
    assert doc2.status == DocumentStatus.DISMISSED
    assert ai1.status == ActionItemStatus.DISMISSED
    assert ai2.status == ActionItemStatus.DISMISSED


@pytest.mark.unit
def test_dismiss_document_updates_status(db_session):
    doc = Document(title="Synthetic Doc", case_id="_TRIAGE")
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    # Add action item
    ai = ActionItem(
        case_id="_TRIAGE",
        source_document_id=doc.id,
        title="Action Doc",
        due_date=datetime.now(UTC),
    )
    db_session.add(ai)
    db_session.commit()

    success = dismiss_bundle(db_session, doc_id=doc.id)
    assert success is True

    db_session.refresh(doc)
    db_session.refresh(ai)
    assert doc.status == DocumentStatus.DISMISSED
    assert ai.status == ActionItemStatus.DISMISSED


@pytest.mark.unit
def test_get_triage_bundles_filters_dismissed(db_session):
    # Active bundle
    batch1 = IngestBatch(source_type=IngestBatchSourceType.EMAIL, subject="Active")
    db_session.add(batch1)
    db_session.commit()
    db_session.refresh(batch1)
    doc1 = Document(
        title="Active Doc",
        ingest_batch_id=batch1.id,
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc1)

    # Dismissed bundle
    batch2 = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        subject="Dismissed",
        status=IngestBatchStatus.DISMISSED,
    )
    db_session.add(batch2)
    db_session.commit()
    db_session.refresh(batch2)
    doc2 = Document(
        title="Dismissed Doc",
        ingest_batch_id=batch2.id,
        case_id="_TRIAGE",
        status=DocumentStatus.DISMISSED,
        needs_review=True,
    )
    db_session.add(doc2)

    db_session.commit()

    bundles = get_triage_bundles(db_session)
    # Should only contain batch1
    batch_ids = [b.batch_id for b in bundles]
    assert batch1.id in batch_ids
    assert batch2.id not in batch_ids
