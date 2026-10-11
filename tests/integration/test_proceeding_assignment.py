"""A proceeding follows its Aktenzeichen; the court level alone never forks one."""

from datetime import UTC, datetime

import pytest

from app.models.database import Case, Document, IngestBatch, Proceeding, User
from app.models.enums import (
    CaseStatus,
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services.ai_summary import _apply_proceeding_extraction
from app.services.proceeding_rematch import (
    fill_proceeding_from_az,
    sync_batch_proceeding,
)

pytestmark = pytest.mark.integration

AZ = "2 K 92/25"


@pytest.fixture
def setup(db_session):
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    db_session.add(
        Case(id="PA-1", title="Teilung", status=CaseStatus.INTAKE, owner_id=owner.id)
    )
    proc = Proceeding(
        case_id="PA-1",
        court_name="Amtsgericht Ingolstadt",
        court_level=ProceedingCourtLevel.AG,
        az_court=AZ,
        status=ProceedingStatus.ACTIVE,
        is_draft=False,
    )
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
        received_at=datetime(2026, 10, 9, tzinfo=UTC),
        case_id="PA-1",
    )
    db_session.add_all([proc, batch])
    db_session.flush()

    def doc(role, proceeding_id=None, az=AZ):
        d = Document(
            title=f"{role.value}",
            owner_id=owner.id,
            case_id="PA-1",
            ingest_batch_id=batch.id,
            role=role,
            originator_type=OriginatorType.COURT,
            az_court=az,
            proceeding_id=proceeding_id,
        )
        db_session.add(d)
        db_session.flush()
        return d

    return db_session, proc, batch, doc


@pytest.mark.parametrize("level", ["lg", "olg", "other"])
def test_same_az_with_other_level_does_not_fork_or_close(setup, level):
    db, proc, _batch, doc = setup
    d = doc(DocumentRole.STANDALONE, proc.id)
    _apply_proceeding_extraction(
        d,
        {
            "is_court_document": True,
            "az_court": AZ,
            "court_level": level,
            "court_name": "Landgericht Ingolstadt",
        },
        db,
    )
    db.flush()
    assert db.query(Proceeding).filter_by(case_id="PA-1").count() == 1
    assert proc.status == ProceedingStatus.ACTIVE
    assert d.proceeding_id == proc.id
    assert proc.court_level == ProceedingCourtLevel.AG


def test_different_az_still_forks_a_new_instance(setup):
    db, proc, _batch, doc = setup
    d = doc(DocumentRole.STANDALONE, proc.id)
    _apply_proceeding_extraction(
        d,
        {
            "is_court_document": True,
            "az_court": "22 T 342/26",
            "court_level": "lg",
            "court_name": "Landgericht Ingolstadt",
        },
        db,
    )
    db.flush()
    assert d.proceeding_id != proc.id
    assert proc.status == ProceedingStatus.CLOSED


def test_fill_assigns_existing_proceeding_by_az_only(setup):
    db, proc, _batch, doc = setup
    cover = doc(DocumentRole.COVER_LETTER)
    other = doc(DocumentRole.ENCLOSURE, az="99 K 1/25")
    assert fill_proceeding_from_az(db, cover) is True
    assert cover.proceeding_id == proc.id
    assert fill_proceeding_from_az(db, other) is False
    assert other.proceeding_id is None
    assert db.query(Proceeding).filter_by(case_id="PA-1").count() == 1


def test_batch_follows_cover_letter_then_majority(setup):
    db, proc, batch, doc = setup
    second = Proceeding(
        case_id="PA-1",
        court_name="Landgericht Ingolstadt",
        court_level=ProceedingCourtLevel.LG,
        az_court="22 T 1/26",
        status=ProceedingStatus.ACTIVE,
        is_draft=False,
    )
    db.add(second)
    db.flush()
    cover = doc(DocumentRole.COVER_LETTER, proc.id)
    doc(DocumentRole.ENCLOSURE, second.id)
    doc(DocumentRole.ENCLOSURE, second.id)
    sync_batch_proceeding(db, batch)
    assert batch.proceeding_id == proc.id  # cover letter wins
    cover.role = DocumentRole.ENCLOSURE
    db.flush()
    sync_batch_proceeding(db, batch)
    assert batch.proceeding_id == second.id  # majority
