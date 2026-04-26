"""Unit tests for neighbor_doc_ids — proceeding-scoped prev/next navigation."""

from datetime import datetime

import pytest

from app.models.database import Case, CaseStatus, Document, Proceeding
from app.models.enums import ProceedingCourtLevel, ProceedingStatus
from app.services.case_dashboard_service import neighbor_doc_ids


@pytest.fixture
def proceeding(db_session):
    case = Case(id="NBR-TEST-001", title="Neighbor Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()
    proc = Proceeding(
        case_id=case.id,
        court_name="Testgericht",
        court_level=ProceedingCourtLevel.AG,
        status=ProceedingStatus.ACTIVE,
    )
    db_session.add(proc)
    db_session.commit()
    return proc


@pytest.mark.unit
def test_neighbor_no_proceeding(db_session):
    doc = Document(title="Standalone", proceeding_id=None)
    db_session.add(doc)
    db_session.commit()

    prev_id, next_id, *_ = neighbor_doc_ids(db_session, doc)
    assert prev_id is None
    assert next_id is None


@pytest.mark.unit
def test_neighbor_single_doc(db_session, proceeding):
    doc = Document(title="Only Doc", proceeding_id=proceeding.id)
    db_session.add(doc)
    db_session.commit()

    prev_id, next_id, *_ = neighbor_doc_ids(db_session, doc)
    assert prev_id is None
    assert next_id is None


@pytest.mark.unit
def test_neighbor_middle_doc(db_session, proceeding):
    d1 = Document(
        title="Doc 1", proceeding_id=proceeding.id, issued_date=datetime(2024, 1, 1)
    )
    d2 = Document(
        title="Doc 2", proceeding_id=proceeding.id, issued_date=datetime(2024, 2, 1)
    )
    d3 = Document(
        title="Doc 3", proceeding_id=proceeding.id, issued_date=datetime(2024, 3, 1)
    )
    db_session.add_all([d1, d2, d3])
    db_session.commit()

    prev_id, next_id, *_ = neighbor_doc_ids(db_session, d2)
    assert prev_id == d1.id
    assert next_id == d3.id


@pytest.mark.unit
def test_neighbor_null_issued_date(db_session, proceeding):
    """Docs without issued_date sort after those with it (nullslast)."""
    d_dated = Document(
        title="Dated", proceeding_id=proceeding.id, issued_date=datetime(2024, 1, 1)
    )
    d_null = Document(title="Null Date", proceeding_id=proceeding.id, issued_date=None)
    db_session.add_all([d_dated, d_null])
    db_session.commit()

    # d_dated comes first; d_null sorts last (null → last)
    prev_id, next_id, *_ = neighbor_doc_ids(db_session, d_null)
    assert prev_id == d_dated.id
    assert next_id is None

    prev_id, next_id, *_ = neighbor_doc_ids(db_session, d_dated)
    assert prev_id is None
    assert next_id == d_null.id


@pytest.mark.unit
def test_neighbor_returns_position_and_total(db_session, proceeding):
    """neighbor_doc_ids must return 1-indexed position and total count."""
    d1 = Document(
        title="First", proceeding_id=proceeding.id, issued_date=datetime(2024, 1, 1)
    )
    d2 = Document(
        title="Second", proceeding_id=proceeding.id, issued_date=datetime(2024, 2, 1)
    )
    d3 = Document(
        title="Third", proceeding_id=proceeding.id, issued_date=datetime(2024, 3, 1)
    )
    db_session.add_all([d1, d2, d3])
    db_session.commit()

    _, _, pos, total = neighbor_doc_ids(db_session, d1)
    assert pos == 1
    assert total == 3

    _, _, pos, total = neighbor_doc_ids(db_session, d2)
    assert pos == 2
    assert total == 3

    _, _, pos, total = neighbor_doc_ids(db_session, d3)
    assert pos == 3
    assert total == 3


@pytest.mark.unit
def test_neighbor_doc_id_mismatch_graceful(db_session, proceeding):
    """neighbor_doc_ids handles gracefully when doc.id is absent from the query results.

    Simulates a stale in-memory doc whose id no longer matches any DB row
    (e.g., after a rollback). The function returns (None, None) via the
    ValueError guard in ids.index().
    """
    # Create one real sibling so the DB query returns non-empty results.
    sibling = Document(title="Sibling", proceeding_id=proceeding.id)
    db_session.add(sibling)
    db_session.commit()

    # Build a transient doc (not in DB) with the same proceeding_id.
    ghost = Document(title="Ghost", proceeding_id=proceeding.id)
    ghost.id = 99999  # not in the DB

    prev_id, next_id, *_ = neighbor_doc_ids(db_session, ghost)
    assert prev_id is None
    assert next_id is None
