"""Regression coverage for enrich_document_with_ai's auto-triage case guard.

Same bypass class as _apply_script_extractors (test_apply_script_extractors_
case_guard.py): AI-extracted `internal_id`/`az_court` (sourced from the
document's own text) auto-triage a _TRIAGE document into an existing case
with no ownership check. Found in PR #160's second review round, in the same
process_document_task pipeline (METADATA stage) as the EXTRACT-stage guard
already fixed there.
"""

import pytest

from app.models.database import Case, Document
from app.models.enums import CaseStatus, Jurisdiction
from app.services import auth_service
from app.services.ai_summary import enrich_document_with_ai


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
def test_ai_auto_triage_cannot_move_doc_into_unowned_case(db_session, two_users):
    a, b = two_users
    _make_case(db_session, "ADV-999-Z", b.id)

    doc = Document(
        title="Forged letter",
        content="Re: ADV-999-Z ongoing matter",
        owner_id=a.id,
        case_id="_TRIAGE",
    )
    db_session.add(doc)
    db_session.commit()

    enrich_document_with_ai(doc, {"internal_id": "ADV-999-Z"}, db_session)

    assert doc.case_id == "_TRIAGE"
    # Must not fall through to auto-creating/reattaching a draft either —
    # the case id already belongs to an existing (blocked) case.
    assert db_session.get(Case, "ADV-999-Z").owner_id == b.id


@pytest.mark.unit
def test_ai_auto_triage_still_moves_doc_when_owner_can_edit_case(db_session, two_users):
    a, _b = two_users
    _make_case(db_session, "ADV-111-A", a.id)

    doc = Document(
        title="Own letter",
        content="Re: ADV-111-A ongoing matter",
        owner_id=a.id,
        case_id="_TRIAGE",
    )
    db_session.add(doc)
    db_session.commit()

    enrich_document_with_ai(doc, {"internal_id": "ADV-111-A"}, db_session)

    assert doc.case_id == "ADV-111-A"


def _family(db, owner_id):
    from app.models.enums import DocumentRole, OriginatorType

    motion = Document(
        title="Antrag",
        content="x",
        owner_id=owner_id,
        case_id="_TRIAGE",
        role=DocumentRole.COVER_LETTER,
        originator_type=OriginatorType.OPPOSING,
    )
    db.add(motion)
    db.flush()
    exhibit = Document(
        title="Old ruling",
        content="y",
        owner_id=owner_id,
        case_id="_TRIAGE",
        parent_id=motion.id,
        role=DocumentRole.ENCLOSURE,
    )
    db.add(exhibit)
    db.commit()
    return motion, exhibit


@pytest.mark.unit
def test_the_family_follows_a_documents_resolved_case(db_session, two_users):
    a, _b = two_users
    _make_case(db_session, "ADV-111-A", a.id)
    motion, exhibit = _family(db_session, a.id)

    enrich_document_with_ai(motion, {"internal_id": "ADV-111-A"}, db_session)

    assert motion.case_id == "ADV-111-A"
    assert exhibit.case_id == "ADV-111-A"
    assert exhibit.extraction_confidence["case_id"] == "inherited"


@pytest.mark.unit
def test_an_exhibit_keeps_the_filings_case_over_its_own_reference(
    db_session, two_users
):
    """The exhibit's own file reference points at another matter; as evidence it
    belongs to the filing it is attached to, and must not move (or draft a case)."""
    a, _b = two_users
    _make_case(db_session, "ADV-111-A", a.id)
    _make_case(db_session, "ADV-222-B", a.id)
    motion, exhibit = _family(db_session, a.id)
    enrich_document_with_ai(motion, {"internal_id": "ADV-111-A"}, db_session)

    enrich_document_with_ai(exhibit, {"internal_id": "ADV-222-B"}, db_session)

    assert exhibit.case_id == "ADV-111-A"
