"""Unit tests for triage_confirmation.reset_and_reenrich.

PR4 (2026-09-27 ingestion audit, F4): reset_and_reenrich used to
unconditionally reset_stage + dispatch ENRICH for every doc whose METADATA
completed, with no check for an ENRICH already in flight — resetting a
RUNNING/RETRYING stage out from under itself races the two dispatches.
"""

import pytest

from app.models.database import Case, Document
from app.models.enums import CaseStatus, Jurisdiction, OriginatorType, PipelineStage
from app.services.pipeline_status import initialize, mark_completed, stages_dict
from app.services.triage_confirmation import reset_and_reenrich


def _make_doc_with_metadata_completed(db_session, case_id):
    doc = Document(
        title="x",
        content="x",
        case_id=case_id,
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    mark_completed(doc.id, PipelineStage.EXTRACT, db_session)
    mark_completed(doc.id, PipelineStage.METADATA, db_session)
    db_session.commit()
    return doc


@pytest.mark.unit
def test_reset_and_reenrich_dispatches_when_enrich_pending(
    db_session, mock_dispatch_task
):
    case = Case(
        id="_TR_REENRICH1",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()
    doc = _make_doc_with_metadata_completed(db_session, case.id)

    reset_and_reenrich(db_session, [doc])

    mock_dispatch_task.assert_called_once()
    dispatched_task, dispatched_doc_id = mock_dispatch_task.call_args[0]
    assert dispatched_doc_id == doc.id
    db_session.expire(doc, ["stage_rows"])
    assert stages_dict(doc)["enrich"]["status"] == "running"


@pytest.mark.unit
def test_reset_and_reenrich_skips_when_enrich_already_running(
    db_session, mock_dispatch_task
):
    """Resetting a RUNNING ENRICH out from under itself would race the two
    dispatches — the in-flight run's own terminal write should be the only
    one that resolves this stage."""
    case = Case(
        id="_TR_REENRICH2",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()
    doc = _make_doc_with_metadata_completed(db_session, case.id)
    from app.services.pipeline_status import mark_started

    mark_started(doc.id, PipelineStage.ENRICH, db_session)

    reset_and_reenrich(db_session, [doc])

    mock_dispatch_task.assert_not_called()
    db_session.expire(doc, ["stage_rows"])
    assert stages_dict(doc)["enrich"]["status"] == "running"


@pytest.mark.unit
def test_reset_and_reenrich_skips_when_enrich_retrying(db_session, mock_dispatch_task):
    case = Case(
        id="_TR_REENRICH3",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()
    doc = _make_doc_with_metadata_completed(db_session, case.id)
    from app.services.pipeline_status import schedule_retry

    schedule_retry(
        doc.id,
        PipelineStage.ENRICH,
        db_session,
        error="transient",
        attempt=1,
        max_attempts=3,
        countdown=30,
    )

    reset_and_reenrich(db_session, [doc])

    mock_dispatch_task.assert_not_called()
    db_session.expire(doc, ["stage_rows"])
    assert stages_dict(doc)["enrich"]["status"] == "retrying"


@pytest.mark.unit
def test_reset_and_reenrich_skips_docs_whose_metadata_did_not_complete(
    db_session, mock_dispatch_task
):
    case = Case(
        id="_TR_REENRICH4",
        title="T",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()
    doc = Document(
        title="x", content="x", case_id=case.id, originator_type=OriginatorType.UNKNOWN
    )
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()

    reset_and_reenrich(db_session, [doc])

    mock_dispatch_task.assert_not_called()
