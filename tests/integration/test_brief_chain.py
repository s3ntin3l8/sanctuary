"""Integration tests for the extract_claims → generate_case_brief task chain."""

from unittest.mock import patch

import pytest

from app.models.database import Case, Document
from app.models.enums import CaseStatus, OriginatorType


@pytest.mark.integration
def test_extract_claims_task_enqueues_brief_for_normal_case(db_session):
    """After extract_claims succeeds, generate_case_brief_task.delay is called with case_id."""
    # Create a real case
    case = Case(id="BRIEF-001", title="Brief Chain Test", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    # Create a document in that case
    doc = Document(
        title="Test Doc",
        content="Some content about the case.",
        case_id="BRIEF-001",
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    doc_id = doc.id

    # _trigger_case_brief does `from app.tasks.generate_case_brief import generate_case_brief_task`
    # then calls `.delay()`. Patch `delay` on the real task object at its module location.
    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.tasks.generate_case_brief.generate_case_brief_task.delay"
        ) as mock_delay,
    ):
        from app.tasks.extract_claims import _trigger_case_brief

        _trigger_case_brief(doc_id)

    mock_delay.assert_called_once_with("BRIEF-001")


@pytest.mark.integration
def test_extract_claims_task_skips_brief_for_triage(db_session):
    """For _TRIAGE documents, generate_case_brief_task.delay is NOT called."""
    # `_TRIAGE` is pre-seeded by the conftest cleanup_per_test fixture.

    doc = Document(
        title="Triage Doc",
        content="Some triage content.",
        case_id="_TRIAGE",
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    doc_id = doc.id

    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.tasks.generate_case_brief.generate_case_brief_task.delay"
        ) as mock_delay,
    ):
        from app.tasks.extract_claims import _trigger_case_brief

        _trigger_case_brief(doc_id)

    mock_delay.assert_not_called()
