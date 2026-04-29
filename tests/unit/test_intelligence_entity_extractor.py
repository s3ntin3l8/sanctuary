"""Tests for entity_extractor._save_entities validation and dedup logic."""

import pytest

from app.models.database import Entity
from app.services.intelligence.entity_extractor import _save_entities


@pytest.mark.unit
def test_save_entities_valid(db_session, sample_document):
    """Valid entities are persisted."""
    sample_document.case_id = "TEST-001"
    db_session.flush()

    result = {
        "entities": [
            {
                "type": "COURT",
                "name": "Amtsgericht Hamburg",
                "context_quote": "before the court",
            },
            {"type": "PERSON", "name": "Dr. Müller", "context_quote": "represented by"},
        ]
    }
    count = _save_entities(sample_document, result, db_session)

    assert count == 2
    entities = db_session.query(Entity).filter(Entity.case_id == "TEST-001").all()
    assert len(entities) == 2
    names = {e.name for e in entities}
    assert "Amtsgericht Hamburg" in names
    assert "Dr. Müller" in names


@pytest.mark.unit
def test_save_entities_dedup(db_session, sample_document):
    """Same case+type+name is not duplicated."""
    # `sample_document` is on `sample_case` (id="TEST-001"); use that for FK validity.
    case_id = sample_document.case_id

    result = {
        "entities": [
            {"type": "COURT", "name": "Amtsgericht Hamburg", "context_quote": "text"},
        ]
    }
    _save_entities(sample_document, result, db_session)
    count2 = _save_entities(sample_document, result, db_session)  # second call
    assert count2 == 0

    total = db_session.query(Entity).filter(Entity.case_id == case_id).count()
    assert total == 1


@pytest.mark.unit
def test_save_entities_unknown_type_skipped(db_session, sample_document):
    """Entities with invalid type strings are silently skipped."""
    result = {
        "entities": [
            {"type": "INVALID_TYPE", "name": "Something", "context_quote": ""},
            {"type": "COURT", "name": "Valid Court", "context_quote": ""},
        ]
    }
    count = _save_entities(sample_document, result, db_session)
    assert count == 1


@pytest.mark.unit
def test_save_entities_empty_name_skipped(db_session, sample_document):
    """Entities with empty names are silently skipped."""
    result = {
        "entities": [
            {"type": "PERSON", "name": "", "context_quote": ""},
            {"type": "PERSON", "name": "Valid Name", "context_quote": ""},
        ]
    }
    count = _save_entities(sample_document, result, db_session)
    assert count == 1


@pytest.mark.unit
def test_save_entities_no_entities(db_session, sample_document):
    """Empty list returns 0."""
    count = _save_entities(sample_document, {"entities": []}, db_session)
    assert count == 0
