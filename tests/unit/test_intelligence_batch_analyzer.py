"""Tests for Phase 4 batch analyzer."""

from datetime import datetime

import pytest

from app.models.database import Document, IngestBatch
from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
)
from app.services.intelligence.batch_analyzer import _apply_batch_results


@pytest.fixture
def batch_with_two_docs(db_session, sample_case):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        sender_email="court@ag-hamburg.de",
        subject="Begleitschreiben + Anlage",
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        created_at=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    cover = Document(
        title="Begleitschreiben",
        content="Im Auftrag des Gerichts übersende ich anliegend...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    enclosure = Document(
        title="Klageerwiderung.pdf",
        content="Die Beklagte widerspricht der Klage...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(cover)
    db_session.add(enclosure)
    db_session.commit()
    db_session.refresh(cover)
    db_session.refresh(enclosure)
    db_session.refresh(batch)
    return batch, cover, enclosure


@pytest.mark.unit
def test_cover_letter_detected(db_session, batch_with_two_docs):
    batch, cover, enclosure = batch_with_two_docs

    result = {
        "cover_letter_doc_id": cover.id,
        "is_cover_letter": True,
        "court_relay": True,
        "enclosed_descriptions": [
            {
                "description": "Klageerwiderung",
                "attributed_originator": "Opposing counsel",
                "originator_type": "opposing",
                "matched_filename": "Klageerwiderung.pdf",
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)

    db_session.expire_all()
    cover = db_session.get(Document, cover.id)
    enclosure = db_session.get(Document, enclosure.id)

    assert cover.role == DocumentRole.COVER_LETTER
    assert cover.court_relay is True
    assert enclosure.role == DocumentRole.ENCLOSURE
    assert enclosure.parent_id == cover.id
    assert enclosure.attributed_originator == "Opposing counsel"
    assert enclosure.originator_type == OriginatorType.OPPOSING


@pytest.mark.unit
def test_single_doc_gets_standalone_role(db_session, sample_case):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.SCAN,
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        created_at=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="Urteil.pdf",
        content="Das Urteil lautet...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    result = {
        "cover_letter_doc_id": None,
        "is_cover_letter": False,
        "court_relay": False,
        "enclosed_descriptions": [],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [doc], result, db_session)

    db_session.expire_all()
    doc = db_session.get(Document, doc.id)
    assert doc.role == DocumentRole.STANDALONE
    assert doc.parent_id is None


@pytest.mark.unit
def test_action_items_created(db_session, sample_case, batch_with_two_docs):
    from app.models.database import ActionItem
    from app.models.enums import ActionItemStatus, ActionItemType

    batch, cover, enclosure = batch_with_two_docs

    result = {
        "cover_letter_doc_id": cover.id,
        "is_cover_letter": True,
        "court_relay": False,
        "enclosed_descriptions": [],
        "detected_actions": [
            {
                "title": "File response",
                "action_type": "deadline",
                "due_date": "2025-06-30",
                "description": "Must respond to opposing motion",
                "confidence": "high",
            }
        ],
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)

    items = (
        db_session.query(ActionItem).filter(ActionItem.case_id == sample_case.id).all()
    )
    assert len(items) == 1
    assert items[0].action_type == ActionItemType.DEADLINE
    assert items[0].title == "File response"
    assert items[0].status == ActionItemStatus.OPEN
    assert items[0].source_document_id == cover.id
