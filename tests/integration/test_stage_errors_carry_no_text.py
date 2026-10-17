"""A pipeline stage error is stored as one bounded line without quoted content."""

import pytest
from sqlalchemy import text

from app.models.database import Document
from app.models.enums import PipelineStage
from app.services.pipeline_status import initialize, mark_failed

pytestmark = pytest.mark.integration

SENTINEL = "Mustermann-Klage-vertraulich"


def _error(db, doc_id) -> str:
    return db.execute(
        text(
            "SELECT error FROM document_pipeline_stages "
            "WHERE document_id = :d AND stage = 'enrich'"
        ),
        {"d": doc_id},
    ).scalar_one()


def test_a_stage_error_is_sanitised_where_it_is_stored(db_session, sample_case):
    doc = Document(title="D", content="x", case_id=sample_case.id)
    db_session.add(doc)
    db_session.flush()
    initialize(doc, batched=False, db=db_session)
    db_session.commit()
    mark_failed(
        doc.id,
        PipelineStage.ENRICH,
        db_session,
        f"1 validation error\ntext [type=t, input_value='{SENTINEL}', input_type=str]"
        + " padding" * 200,
    )
    stored = _error(db_session, doc.id)
    assert SENTINEL not in stored
    assert "\n" not in stored and len(stored) <= 300
