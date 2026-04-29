"""Test the idempotent _TRIAGE singleton seed."""

from app.models.database import Case
from app.services.case_service import seed_triage_case


def test_seed_triage_case_creates_when_missing(db_session):
    # The conftest pre-seeds `_TRIAGE` per test (FK enforcement requires it).
    # Delete it so we can verify the create path.
    db_session.query(Case).filter_by(id="_TRIAGE").delete()
    db_session.commit()
    assert db_session.query(Case).filter_by(id="_TRIAGE").first() is None

    seed_triage_case(db_session)

    triage = db_session.query(Case).filter_by(id="_TRIAGE").first()
    assert triage is not None
    assert triage.title == "Triage Inbox"


def test_seed_triage_case_is_idempotent(db_session):
    seed_triage_case(db_session)
    seed_triage_case(db_session)
    seed_triage_case(db_session)

    count = db_session.query(Case).filter_by(id="_TRIAGE").count()
    assert count == 1
