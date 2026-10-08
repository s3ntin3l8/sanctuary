from datetime import timedelta

import pytest

from app.core.timezone import now_utc
from app.models.database import ActionItem, Document
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    OriginatorType,
    SignificanceTier,
)
from app.services.intelligence.chronology_context import format_chronology_for_case


def _item(case_id, title, due, status=ActionItemStatus.OPEN, doc_id=None, kind=None):
    return ActionItem(
        case_id=case_id,
        title=title,
        due_date=due,
        status=status,
        action_type=kind or ActionItemType.DEADLINE,
        source_document_id=doc_id,
    )


def _doc(case_id, title, issued, tier=SignificanceTier.INFORMATIONAL):
    return Document(
        case_id=case_id,
        title=title,
        issued_date=issued,
        ingest_date=issued,
        originator_type=OriginatorType.COURT,
        significance_tier=tier,
    )


@pytest.mark.unit
def test_empty_case_returns_empty_string(db_session, sample_case):
    assert format_chronology_for_case(db_session, sample_case.id) == ""


@pytest.mark.unit
def test_deadline_states_and_citations(db_session, sample_case):
    now = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    doc = _doc(sample_case.id, "Ladung", now - timedelta(days=60))
    db_session.add(doc)
    db_session.flush()
    db_session.add_all(
        [
            _item(
                sample_case.id, "Late filing", now - timedelta(days=10), doc_id=doc.id
            ),
            _item(
                sample_case.id,
                "Filed on time",
                now - timedelta(days=30),
                status=ActionItemStatus.COMPLETED,
            ),
            _item(
                sample_case.id,
                "Dropped",
                now - timedelta(days=20),
                status=ActionItemStatus.DISMISSED,
            ),
            _item(
                sample_case.id,
                "Hearing",
                now + timedelta(days=15),
                kind=ActionItemType.COURT_DATE,
            ),
        ]
    )
    db_session.commit()

    block = format_chronology_for_case(db_session, sample_case.id)

    assert block.startswith("Case chronology (oldest first):")
    assert "OVERDUE (open, 10 days past due) Late filing" in block
    assert f"[DOC:{doc.id}]" in block
    assert "done Filed on time" in block
    assert "UPCOMING Hearing" in block
    assert "Dropped" not in block
    assert "missed" not in block.lower()
    # no citation tag for an item without a source document
    assert "Filed on time [DOC" not in block
    assert block.index("Filed on time") < block.index("Late filing")


@pytest.mark.unit
def test_silence_line_between_distant_events(db_session, sample_case):
    now = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    db_session.add_all(
        [
            _doc(sample_case.id, "Old", now - timedelta(days=100)),
            _doc(sample_case.id, "New", now - timedelta(days=10)),
        ]
    )
    db_session.commit()

    block = format_chronology_for_case(db_session, sample_case.id)

    assert "… 90 days without activity" in block
    assert (
        block.index("Old") < block.index("days without activity") < block.index("New")
    )


@pytest.mark.unit
def test_trimming_drops_minor_documents_first_and_keeps_deadlines(
    db_session, sample_case
):
    now = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    db_session.add_all(
        [
            _doc(sample_case.id, f"Minor {i}", now - timedelta(days=i + 1))
            for i in range(6)
        ]
        + [
            _doc(
                sample_case.id,
                "Critical",
                now - timedelta(days=50),
                SignificanceTier.CRITICAL,
            )
        ]
        + [_item(sample_case.id, "Deadline", now + timedelta(days=5))]
    )
    db_session.commit()

    block = format_chronology_for_case(db_session, sample_case.id, max_events=3)

    assert "Critical" in block
    assert "Deadline" in block
    assert block.count("Minor") == 1
    assert "(+5 lower-priority events omitted)" in block
