from unittest.mock import patch

import pytest

from app.models.database import Document, Proceeding
from app.models.enums import (
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services.intelligence.proceeding_analyzer import analyze_and_update_proceeding
from app.services.intelligence.schemas import ProceedingExtraction


@pytest.fixture
def sample_proceeding(db_session, sample_case):
    proc = Proceeding(
        case_id=sample_case.id,
        court_name="Unknown Court",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
    )
    db_session.add(proc)
    db_session.commit()
    db_session.refresh(proc)
    return proc


@pytest.fixture
def doc_with_proceeding(db_session, sample_case, sample_proceeding):
    doc = Document(
        title="Test Court Doc",
        content="This is a long enough content for a court document analysis test. "
        * 5,
        case_id=sample_case.id,
        proceeding_id=sample_proceeding.id,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


@pytest.mark.unit
@patch("app.services.intelligence.proceeding_analyzer.call_json_ai")
def test_autofill_empty_proceeding(
    mock_llm, db_session, doc_with_proceeding, sample_proceeding
):
    mock_llm.return_value = ProceedingExtraction.model_validate(
        {
            "is_court_document": True,
            "court_level": "ag",
            "court_name": "Amtsgericht Hamburg",
            "az_court": "003 F 426/25",
            "subject_matter": "Custody",
            "appeal_deadline_days": None,
        }
    )

    result = analyze_and_update_proceeding(
        doc_with_proceeding, "test-model", db_session
    )

    assert result is None
    db_session.refresh(sample_proceeding)
    assert sample_proceeding.court_name == "Amtsgericht Hamburg"
    assert sample_proceeding.az_court == "003 F 426/25"
    assert sample_proceeding.subject_matter == "Custody"


@pytest.mark.unit
@patch("app.services.intelligence.proceeding_analyzer.call_json_ai")
def test_escalation_to_new_proceeding(
    mock_llm, db_session, doc_with_proceeding, sample_proceeding
):
    # Initial state: AG
    sample_proceeding.court_level = ProceedingCourtLevel.AG
    sample_proceeding.az_court = "OLD-AZ"
    db_session.commit()

    mock_llm.return_value = ProceedingExtraction.model_validate(
        {
            "is_court_document": True,
            "court_level": "olg",
            "court_name": "Hanseatisches Oberlandesgericht",
            "az_court": "12 UF 123/25",
            "subject_matter": "Appeal",
            "appeal_deadline_days": 30,
        }
    )

    result = analyze_and_update_proceeding(
        doc_with_proceeding, "test-model", db_session
    )

    assert result is None

    # Old proceeding should be closed
    db_session.refresh(sample_proceeding)
    assert sample_proceeding.status == ProceedingStatus.CLOSED
    assert sample_proceeding.ended_at is not None

    # New proceeding should be created
    new_proc = (
        db_session.query(Proceeding)
        .filter(Proceeding.court_level == ProceedingCourtLevel.OLG)
        .first()
    )
    assert new_proc is not None
    assert new_proc.status == ProceedingStatus.ACTIVE
    assert new_proc.az_court == "12 UF 123/25"

    # Document should be re-assigned
    db_session.refresh(doc_with_proceeding)
    assert doc_with_proceeding.proceeding_id == new_proc.id


@pytest.mark.unit
@patch("app.services.intelligence.proceeding_analyzer.call_json_ai")
def test_not_a_court_document(mock_llm, db_session, doc_with_proceeding):
    mock_llm.return_value = ProceedingExtraction.model_validate(
        {"is_court_document": False}
    )

    result = analyze_and_update_proceeding(
        doc_with_proceeding, "test-model", db_session
    )
    assert result == "not a court document"


@pytest.mark.unit
def test_content_too_short(db_session, doc_with_proceeding):
    doc_with_proceeding.content = "Short"
    db_session.commit()

    result = analyze_and_update_proceeding(
        doc_with_proceeding, "test-model", db_session
    )
    assert result == "content too short"
