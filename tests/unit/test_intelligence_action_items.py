"""Tests for create_from_payload in action_items.py — specifically the past-event guard."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.database import ActionItem
from app.services.intelligence.action_items import create_from_payload


@pytest.fixture
def future_action(sample_doc_date):
    """A well-formed action item dated 30 days after the document."""
    return {
        "title": "Gerichtstermin",
        "action_type": "court_date",
        "due_date": (sample_doc_date + timedelta(days=30)).strftime("%Y-%m-%d"),
        "description": "Scheduled hearing",
        "confidence": "high",
    }


@pytest.fixture
def past_action(sample_doc_date):
    """An action item dated 5 days BEFORE the document — stale past-event extraction."""
    return {
        "title": "Heutiger Termin",
        "action_type": "court_date",
        "due_date": (sample_doc_date - timedelta(days=5)).strftime("%Y-%m-%d"),
        "description": "Sitzungsprotokoll - hearing already occurred",
        "confidence": "high",
    }


@pytest.fixture
def same_day_action(sample_doc_date):
    """An action item on the same day as the document — within the 1-day buffer, must pass."""
    return {
        "title": "Termin heute",
        "action_type": "court_date",
        "due_date": sample_doc_date.strftime("%Y-%m-%d"),
        "description": "Same day as document",
        "confidence": "medium",
    }


@pytest.fixture
def sample_doc_date():
    return datetime(2026, 3, 15)


@pytest.mark.unit
def test_future_action_item_is_kept(
    db_session, sample_case, future_action, sample_doc_date
):
    count = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=None,
        proceeding_id=None,
        actions=[future_action],
        db=db_session,
        source_doc_date=sample_doc_date,
    )
    assert count == 1
    db_session.flush()
    items = (
        db_session.query(ActionItem).filter(ActionItem.case_id == sample_case.id).all()
    )
    assert len(items) == 1
    assert items[0].title == "Gerichtstermin"


@pytest.mark.unit
def test_past_action_item_is_dropped(
    db_session, sample_case, past_action, sample_doc_date
):
    count = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=None,
        proceeding_id=None,
        actions=[past_action],
        db=db_session,
        source_doc_date=sample_doc_date,
    )
    assert count == 0
    db_session.flush()
    items = (
        db_session.query(ActionItem).filter(ActionItem.case_id == sample_case.id).all()
    )
    assert len(items) == 0


@pytest.mark.unit
def test_same_day_action_item_is_kept(
    db_session, sample_case, same_day_action, sample_doc_date
):
    """Actions on the same day as the document must pass (within the 1-day buffer)."""
    count = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=None,
        proceeding_id=None,
        actions=[same_day_action],
        db=db_session,
        source_doc_date=sample_doc_date,
    )
    assert count == 1


@pytest.mark.unit
def test_guard_skipped_when_no_source_doc_date(db_session, sample_case, past_action):
    """When source_doc_date is None (e.g. batch analysis with no cover-letter date),
    the guard must be skipped and the item inserted as before."""
    count = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=None,
        proceeding_id=None,
        actions=[past_action],
        db=db_session,
        source_doc_date=None,
    )
    assert count == 1


@pytest.mark.unit
def test_mixed_actions_partial_filtering(
    db_session, sample_case, future_action, past_action, sample_doc_date
):
    """Only the past-dated item is dropped; future item survives."""
    count = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=None,
        proceeding_id=None,
        actions=[future_action, past_action],
        db=db_session,
        source_doc_date=sample_doc_date,
    )
    assert count == 1
    db_session.flush()
    items = (
        db_session.query(ActionItem).filter(ActionItem.case_id == sample_case.id).all()
    )
    assert len(items) == 1
    assert items[0].title == "Gerichtstermin"


@pytest.mark.unit
def test_guard_handles_tzaware_source_doc_date(
    db_session, sample_case, past_action, future_action, sample_doc_date
):
    """Production passes `doc.issued_date` which is tz-aware UTC; the AI's
    due_date string parses as naive. The guard must compare on `.date()` so
    mixing the two doesn't raise TypeError."""
    tz_aware = sample_doc_date.replace(tzinfo=UTC)
    count = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=None,
        proceeding_id=None,
        actions=[past_action, future_action],
        db=db_session,
        source_doc_date=tz_aware,
    )
    assert count == 1


@pytest.mark.unit
def test_cross_document_dedup_skips_duplicate(
    db_session, sample_case, future_action, sample_doc_date
):
    """Second document in the same batch (e.g. Verfügung after cover letter)
    must not create a duplicate action item for the same (date, type)."""
    from app.models.database import Document
    from app.models.enums import OriginatorType

    doc_a = Document(
        title="Ladungsschreiben",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
    )
    doc_b = Document(
        title="Verfügung Ladung",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all([doc_a, doc_b])
    db_session.flush()

    # First document stores the action item.
    count1 = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=doc_a.id,
        proceeding_id=None,
        actions=[future_action],
        db=db_session,
        source_doc_date=sample_doc_date,
    )
    db_session.flush()
    assert count1 == 1

    # Second document extracts the same date+type — must be skipped.
    count2 = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=doc_b.id,
        proceeding_id=None,
        actions=[future_action],
        db=db_session,
        source_doc_date=sample_doc_date,
    )
    db_session.flush()
    assert count2 == 0
    total = (
        db_session.query(ActionItem)
        .filter(ActionItem.case_id == sample_case.id)
        .count()
    )
    assert total == 1


@pytest.mark.unit
def test_supersedes_deletes_across_action_types(db_session, sample_case):
    """IB-0033 regression: doc 97 emitted the original hearing as DEADLINE on
    2025-09-15; doc 95 (Terminsverlegung) emitted COURT_DATE on 2025-09-22
    with supersedes_date=2025-09-15. The old DEADLINE must be removed even
    though the new item has a different action_type — the AI classifies the
    same real-world hearing inconsistently across docs."""
    from app.models.database import Document
    from app.models.enums import OriginatorType

    doc_old = Document(
        title="Ladung Erörterungstermin",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
    )
    doc_new = Document(
        title="Terminsverlegung",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all([doc_old, doc_new])
    db_session.flush()

    create_from_payload(
        case_id=sample_case.id,
        source_doc_id=doc_old.id,
        proceeding_id=None,
        actions=[
            {
                "title": "Erörterungstermin",
                "action_type": "deadline",
                "due_date": "2025-09-15",
                "description": "original",
                "confidence": "high",
            }
        ],
        db=db_session,
    )
    db_session.flush()

    create_from_payload(
        case_id=sample_case.id,
        source_doc_id=doc_new.id,
        proceeding_id=None,
        actions=[
            {
                "title": "Erörterungstermin (verlegt)",
                "action_type": "court_date",
                "due_date": "2025-09-22",
                "description": "rescheduled",
                "confidence": "high",
                "supersedes_date": "2025-09-15",
            }
        ],
        db=db_session,
    )
    db_session.flush()

    items = (
        db_session.query(ActionItem).filter(ActionItem.case_id == sample_case.id).all()
    )
    assert len(items) == 1
    assert items[0].due_date.date().isoformat() == "2025-09-22"


@pytest.mark.unit
def test_iso_datetime_due_date_is_parsed(db_session, sample_case):
    """Batch analyzer may return full ISO datetimes; truncation to YYYY-MM-DD
    must allow these to be parsed and stored correctly."""
    action = {
        "title": "Anhörungstermin",
        "action_type": "court_date",
        "due_date": "2025-09-22T10:00:00+02:00",
        "description": "Hearing at Amtsgericht",
        "confidence": "high",
    }
    count = create_from_payload(
        case_id=sample_case.id,
        source_doc_id=None,
        proceeding_id=None,
        actions=[action],
        db=db_session,
    )
    assert count == 1
    db_session.flush()
    item = (
        db_session.query(ActionItem).filter(ActionItem.case_id == sample_case.id).one()
    )
    assert item.due_date.date().isoformat() == "2025-09-22"
