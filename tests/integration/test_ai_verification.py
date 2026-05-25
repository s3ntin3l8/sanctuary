"""Integration tests for AI verification of heuristics."""

from datetime import UTC, datetime

import pytest

from app.models.database import Document
from app.models.enums import OriginatorType
from app.services.ai_summary import _summarize_document_sync


@pytest.mark.integration
def test_ai_verifies_and_overrides_heuristics(db_session, monkeypatch):
    # 1. Setup a doc with WRONG heuristic data
    doc = Document(
        title="Heuristic Test",
        content="This is a court document from AG Hamburg dated 2025-05-20.",
        sender="Wrong Sender",
        received_date=datetime(2020, 1, 1, tzinfo=UTC),
        originator_type=OriginatorType.OPPOSING,
        extraction_confidence={"sender": "low", "issued_date": "low"},
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    # 2. Mock generate_summary_sync to return CORRECT data with high confidence
    def mock_generate_summary_sync(d, db=None):
        return {
            "az_court": "001 F 123/25",
            "internal_id": "8124/25",
            "sender": "AG Hamburg",
            "issued_date": "2025-05-20",
            "originator_type": "court",
            "confidence": {
                "az_court": "high",
                "sender": "high",
                "issued_date": "high",
                "originator_type": "high",
            },
        }

    monkeypatch.setattr(
        "app.services.ai_summary.generate_summary_sync", mock_generate_summary_sync
    )

    # 3. Trigger summary (Phase 1)
    _summarize_document_sync(doc.id, db_session)

    # 4. Verify overrides
    db_session.refresh(doc)
    assert doc.sender == "AG Hamburg"
    assert doc.issued_date.strftime("%Y-%m-%d") == "2025-05-20"
    assert doc.originator_type == OriginatorType.COURT
    # az_court lives on Proceeding, not Document — verify triage matching instead
    assert doc.extraction_confidence["sender"] == "high"
    assert doc.extraction_confidence["issued_date"] == "high"
    assert doc.extraction_confidence["originator_type"] == "high"
    assert doc.extraction_confidence["az_court"] == "high"
