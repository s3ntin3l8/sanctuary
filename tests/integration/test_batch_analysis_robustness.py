"""Integration tests for batch analysis robustness."""

from datetime import UTC, datetime

import pytest

from app.models.database import ActionItem, Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.services.intelligence.batch_analyzer import _apply_batch_results, analyze


@pytest.fixture
def batch_with_failed_doc(db_session, sample_case):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        sender_email="test@example.com",
        subject="Batch with failure",
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(UTC),
    )
    db_session.add(batch)
    db_session.flush()

    # Doc 1: Successful conversion (Cover)
    doc1 = Document(
        title="Cover.pdf",
        content="This is the cover letter. It encloses Doc 2.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
    )
    # Doc 2: Successful conversion (Enclosure)
    doc2 = Document(
        title="Enclosure.pdf",
        content="This is the enclosure content.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
    )
    # Doc 3: Failed conversion (simulating Docling error)
    doc3 = Document(
        title="Corrupted.pdf",
        content=None,
        ingest_batch_id=batch.id,
    )

    db_session.add(doc1)
    db_session.add(doc2)
    db_session.add(doc3)
    db_session.commit()
    return batch, doc1, doc2, doc3


@pytest.mark.integration
def test_batch_analysis_skips_failed_docs(
    db_session, batch_with_failed_doc, monkeypatch
):
    batch, doc1, doc2, doc3 = batch_with_failed_doc

    # Mock the AI call to return a result based only on doc1 and doc2
    def mock_call_batch_analyzer_sync(docs, batch_id, model=None, db=None):
        doc_ids = {d.id for d in docs}
        # Verify that doc1 and doc2 are present
        assert doc1.id in doc_ids
        assert doc2.id in doc_ids
        # Verify that doc3 is NOT present (it has no content)
        assert doc3.id not in doc_ids

        return {
            "bundles": [
                {
                    "cover_letter_doc_id": doc1.id,
                    "enclosed": [
                        {
                            "description": "Enclosure",
                            "matched_filename": doc2.title,
                            "attributed_originator": None,
                            "originator_type": "unknown",
                        }
                    ],
                }
            ],
            "detected_actions": [],
        }

    monkeypatch.setattr(
        "app.services.intelligence.batch_analyzer._call_batch_analyzer_sync",
        mock_call_batch_analyzer_sync,
    )
    monkeypatch.setattr(
        "app.services.intelligence.batch_analyzer.SessionLocal", lambda: db_session
    )
    # Prevent analyze() from closing our test session
    monkeypatch.setattr(db_session, "close", lambda: None)

    # Trigger analysis
    analyze(batch.id)

    db_session.refresh(doc1)
    db_session.refresh(doc2)
    db_session.refresh(doc3)

    assert doc1.role.value == "cover_letter"
    assert doc2.role.value == "enclosure"
    assert doc2.parent_id == doc1.id
    assert doc3.role.value == "standalone"


@pytest.mark.integration
def test_action_items_created_for_triage_case(db_session):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id="_TRIAGE",
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(UTC),
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="Triage Doc",
        content="Deadline: 2025-12-31",
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
    )
    db_session.add(doc)
    db_session.commit()

    result = {
        "cover_letter_doc_id": doc.id,
        "is_cover_letter": True,
        "court_relay": False,
        "enclosed_descriptions": [],
        "detected_actions": [
            {
                "title": "Triage Deadline",
                "action_type": "deadline",
                "due_date": "2025-12-31",
                "description": "Found in triage",
                "confidence": "high",
            }
        ],
    }

    _apply_batch_results(batch.id, [doc], result, db_session)

    # Verify action item is created even for _TRIAGE case
    # Expire to force reload from DB
    db_session.expire_all()
    items = db_session.query(ActionItem).filter(ActionItem.case_id == "_TRIAGE").all()
    assert len(items) == 1
    assert items[0].title == "Triage Deadline"
    assert items[0].source_document_id == doc.id
