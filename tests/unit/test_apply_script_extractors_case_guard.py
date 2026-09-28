"""Regression coverage for _apply_script_extractors' case-id auto-assign guard.

ingest_file() checks access_service.can_edit_case before honoring a case id
sniffed from an upload's filename — but every document also passes through
_apply_script_extractors during background processing (process_document_task
-> process_uploaded_document), which re-derives a case id from the filename
AND body content and, before this fix, applied it with no ownership check at
all. That made the upload-time guard bypassable one Celery hop later. These
tests call _apply_script_extractors directly (no dispatch mocking) so they
actually exercise the code path the bypass lived in.
"""

import pytest

from app.models.database import Case
from app.models.enums import CaseStatus, Jurisdiction
from app.services import auth_service
from app.services.ingestion.service import _apply_script_extractors


def _make_case(db, case_id, owner_id):
    case = Case(
        id=case_id,
        title=f"Case {case_id}",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        owner_id=owner_id,
    )
    db.add(case)
    db.commit()
    return case


@pytest.fixture
def two_users(db_session):
    a = auth_service.create_user(
        db_session, email="a@example.com", password="password123"
    )
    b = auth_service.create_user(
        db_session, email="b@example.com", password="password123"
    )
    db_session.commit()
    return a, b


@pytest.mark.unit
def test_filename_sniff_cannot_reassign_doc_into_unowned_case(db_session, two_users):
    from app.models.database import Document

    a, b = two_users
    _make_case(db_session, "ADV-999-Z", b.id)

    doc = Document(
        title="Forged letter",
        owner_id=a.id,
        case_id="_TRIAGE",
        file_path="_TRIAGE/ADV-999-Z-forged-letter.pdf",
    )
    db_session.add(doc)
    db_session.commit()

    _apply_script_extractors(doc, "", db_session)

    assert doc.case_id == "_TRIAGE"


@pytest.mark.unit
def test_body_content_sniff_cannot_reassign_doc_into_unowned_case(
    db_session, two_users
):
    """Same guard, exercised via the Aktenzeichen/content path rather than
    the filename path — extract_case_id checks both."""
    from app.models.database import Document

    a, b = two_users
    _make_case(db_session, "ADV-999-Z", b.id)

    doc = Document(
        title="Forged letter",
        owner_id=a.id,
        case_id="_TRIAGE",
        file_path="_TRIAGE/letter.pdf",
    )
    db_session.add(doc)
    db_session.commit()

    _apply_script_extractors(doc, "Re: ADV-999-Z ongoing matter", db_session)

    assert doc.case_id == "_TRIAGE"


@pytest.mark.unit
def test_filename_sniff_still_assigns_when_owner_can_edit_case(db_session, two_users):
    a, _b = two_users
    _make_case(db_session, "ADV-111-A", a.id)

    from app.models.database import Document

    doc = Document(
        title="Own letter",
        owner_id=a.id,
        case_id="_TRIAGE",
        file_path="_TRIAGE/ADV-111-A-own-letter.pdf",
    )
    db_session.add(doc)
    db_session.commit()

    _apply_script_extractors(doc, "", db_session)

    assert doc.case_id == "ADV-111-A"
