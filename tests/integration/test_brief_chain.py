"""Integration tests for the extract_claims → generate_case_brief task chain.

Post-redesign (2026-05-22): _trigger_case_brief is a fan-in trigger. It only
dispatches the brief task when EVERY doc in the case has CLAIMS in a terminal
state (completed/failed/skipped), via the atomic claim_case_brief_for_dispatch
orchestrator. Tests must set up CLAIMS pipeline rows accordingly.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models.database import Case, Document
from app.models.enums import CaseStatus, OriginatorType


def _set_claims_status(db_session, doc_id: int, status: str) -> None:
    """Insert or update the CLAIMS pipeline row for a doc."""
    db_session.execute(
        text(
            """
            INSERT INTO document_pipeline_stages (document_id, stage, status)
            VALUES (:doc_id, 'claims', :status)
            ON CONFLICT(document_id, stage) DO UPDATE SET status=:status
            """
        ),
        {"doc_id": doc_id, "status": status},
    )
    db_session.commit()


@pytest.mark.integration
def test_extract_claims_task_enqueues_brief_when_last_sibling_done(db_session):
    """When the trigger fires for the LAST sibling-doc to reach CLAIMS-terminal,
    generate_case_brief_task.delay is called exactly once."""
    case = Case(id="BRIEF-001", title="Brief Chain Test", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

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
    _set_claims_status(db_session, doc_id, "completed")

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
def test_extract_claims_task_skips_brief_while_sibling_pending(db_session):
    """When a sibling-doc in the same case has CLAIMS still pending,
    generate_case_brief_task.delay is NOT called — the orchestrator's
    readiness predicate must hold for the whole case."""
    case = Case(id="BRIEF-002", title="Brief Chain Test 2", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    sibling = Document(
        title="Sibling still processing",
        content="x",
        case_id="BRIEF-002",
        originator_type=OriginatorType.COURT,
    )
    me = Document(
        title="Me — just finished claims",
        content="y",
        case_id="BRIEF-002",
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all([sibling, me])
    db_session.commit()
    db_session.refresh(sibling)
    db_session.refresh(me)

    # Only my CLAIMS is done. Sibling has no terminal claims row.
    _set_claims_status(db_session, me.id, "completed")

    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.tasks.generate_case_brief.generate_case_brief_task.delay"
        ) as mock_delay,
    ):
        from app.tasks.extract_claims import _trigger_case_brief

        _trigger_case_brief(me.id)

    mock_delay.assert_not_called()


@pytest.mark.integration
def test_extract_claims_task_dedups_concurrent_triggers(db_session):
    """Two triggers fired in succession for the same case must produce
    exactly ONE delay() call — the atomic CAS on cases.brief_queued_at
    collapses the second trigger."""
    case = Case(id="BRIEF-003", title="Brief Chain Test 3", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    doc_a = Document(
        title="Doc A",
        content="a",
        case_id="BRIEF-003",
        originator_type=OriginatorType.COURT,
    )
    doc_b = Document(
        title="Doc B",
        content="b",
        case_id="BRIEF-003",
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all([doc_a, doc_b])
    db_session.commit()
    db_session.refresh(doc_a)
    db_session.refresh(doc_b)
    _set_claims_status(db_session, doc_a.id, "completed")
    _set_claims_status(db_session, doc_b.id, "completed")

    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.tasks.generate_case_brief.generate_case_brief_task.delay"
        ) as mock_delay,
    ):
        from app.tasks.extract_claims import _trigger_case_brief

        _trigger_case_brief(doc_a.id)
        _trigger_case_brief(doc_b.id)

    mock_delay.assert_called_once_with("BRIEF-003")


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
