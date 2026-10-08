"""confirm_document stamps user-edited fields with the same confidence keys
METADATA uses, so a "user_set" mark lines up with the AI's own entries."""

import pytest

from app.models.database import Case, Document
from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
from app.services.triage_confirmation import confirm_document


@pytest.mark.unit
def test_finalize_stamps_originator_type_key_as_user_set(db_session):
    db_session.add(
        Case(
            id="_CD_US1",
            title="T",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    doc = Document(
        title="x",
        content="x",
        case_id="_CD_US1",
        originator_type=OriginatorType.UNKNOWN,
        extraction_confidence={"originator_type": "low"},
    )
    db_session.add(doc)
    db_session.commit()

    confirm_document(
        db_session,
        doc.id,
        originator_type=OriginatorType.OWN,
        sender="Kanzlei X",
        finalize=True,
    )

    db_session.refresh(doc)
    assert doc.extraction_confidence["originator_type"] == "user_set"
    assert doc.extraction_confidence["sender"] == "user_set"
    assert "originator" not in doc.extraction_confidence
