"""A METADATA re-run must not overwrite fields the user set in triage."""

from datetime import UTC, datetime

import pytest

from app.models.database import Document
from app.models.enums import OriginatorType
from app.services.ai_summary import enrich_document_with_ai

_AI = {
    "sender": "Amtsgericht Ingolstadt",
    "originator_type": "court",
    "issued_date": "2026-01-02",
    "confidence": {
        "sender": "high",
        "originator_type": "high",
        "issued_date": "high",
    },
}


def _doc(db_session, confidence):
    doc = Document(
        title="x",
        content="x",
        sender="Kanzlei X",
        originator_type=OriginatorType.OWN,
        issued_date=datetime(2026, 4, 30, tzinfo=UTC),
        extraction_confidence=confidence,
    )
    db_session.add(doc)
    db_session.commit()
    return doc


@pytest.mark.unit
def test_user_set_fields_survive_a_metadata_rerun(db_session):
    doc = _doc(
        db_session,
        {
            "sender": "user_set",
            "originator_type": "user_set",
            "issued_date": "user_set",
        },
    )

    enrich_document_with_ai(doc, dict(_AI), db_session)

    assert doc.sender == "Kanzlei X"
    assert doc.originator_type == OriginatorType.OWN
    assert doc.issued_date.date().isoformat() == "2026-04-30"
    assert doc.extraction_confidence == {
        "sender": "user_set",
        "originator_type": "user_set",
        "issued_date": "user_set",
    }


@pytest.mark.unit
def test_ai_still_overwrites_fields_the_user_did_not_set(db_session):
    doc = _doc(db_session, {"sender": "user_set", "originator_type": "low"})

    enrich_document_with_ai(doc, dict(_AI), db_session)

    assert doc.sender == "Kanzlei X"  # user-set, kept
    assert doc.originator_type == OriginatorType.COURT  # AI-set, replaced
    assert doc.issued_date.date().isoformat() == "2026-01-02"
    assert doc.extraction_confidence["sender"] == "user_set"
    assert doc.extraction_confidence["originator_type"] == "high"
