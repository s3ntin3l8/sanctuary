"""Slicing prep progress is merged into meta, never replacing the recovery clock."""

import pytest

from app.models.database import IngestBatch, User
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.services.ingestion.slicer import _write_progress


@pytest.mark.unit
def test_progress_keeps_dispatch_metadata(db_session):
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.SCAN,
        status=IngestBatchStatus.AWAITING_SLICING,
        meta={"slicing": {"status": "preparing", "dispatched_at": "2026-10-09T10:00"}},
    )
    db_session.add(batch)
    db_session.commit()

    _write_progress(db_session, batch, 5, 69, "ocr")

    db_session.refresh(batch)
    slicing = batch.meta["slicing"]
    assert slicing["dispatched_at"] == "2026-10-09T10:00"
    assert slicing["status"] == "preparing"
    assert slicing["progress"] == {"done": 5, "total": 69, "phase": "ocr"}
