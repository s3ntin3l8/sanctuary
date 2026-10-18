from datetime import datetime

import pytest

from app.models.database import ActionItem, Document
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    DocumentType,
    OriginatorType,
)
from app.services.case_timeline_service import CaseTimelineService


@pytest.mark.unit
def test_empty_case_returns_zero_events(db_session, sample_case):
    svc = CaseTimelineService(db_session)
    payload = svc.build_payload(sample_case.id)

    assert payload["total_count"] == 0
    assert payload["events"] == []
    assert payload["month_buckets"] == []
    assert payload["quiet_gaps"] == {}


@pytest.mark.unit
def test_events_sorted_ascending(db_session, sample_case):
    d1 = Document(
        case_id=sample_case.id,
        title="Older",
        issued_date=datetime(2025, 1, 1),
        ingest_date=datetime(2025, 1, 2),
        originator_type=OriginatorType.OWN,
    )
    d2 = Document(
        case_id=sample_case.id,
        title="Newer",
        issued_date=datetime(2025, 6, 1),
        ingest_date=datetime(2025, 6, 2),
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all([d1, d2])
    db_session.commit()

    svc = CaseTimelineService(db_session)
    payload = svc.build_payload(sample_case.id)

    events = payload["events"]
    assert len(events) == 2
    assert events[0].date < events[1].date
    assert events[0].title == "Older"
    assert events[1].title == "Newer"


@pytest.mark.unit
def test_actor_derived_from_originator_type(db_session, sample_case):
    docs = [
        Document(
            case_id=sample_case.id,
            title="Own doc",
            issued_date=datetime(2025, 2, 1),
            ingest_date=datetime.now(),
            originator_type=OriginatorType.OWN,
        ),
        Document(
            case_id=sample_case.id,
            title="Court doc",
            issued_date=datetime(2025, 2, 2),
            ingest_date=datetime.now(),
            originator_type=OriginatorType.COURT,
        ),
        Document(
            case_id=sample_case.id,
            title="Opposing doc",
            issued_date=datetime(2025, 2, 3),
            ingest_date=datetime.now(),
            originator_type=OriginatorType.OPPOSING,
        ),
        Document(
            case_id=sample_case.id,
            title="Third doc",
            issued_date=datetime(2025, 2, 4),
            ingest_date=datetime.now(),
            originator_type=OriginatorType.THIRD_PARTY,
        ),
    ]
    db_session.add_all(docs)
    db_session.commit()

    svc = CaseTimelineService(db_session)
    events = svc.build_payload(sample_case.id)["events"]

    by_title = {e.title: e for e in events}
    assert by_title["Own doc"].actor == "own"
    assert by_title["Court doc"].actor == "court"
    assert by_title["Opposing doc"].actor == "opposing"
    assert by_title["Third doc"].actor == "third"


@pytest.mark.unit
def test_overdue_action_item_flagged(db_session, sample_case):
    past_date = datetime(2024, 1, 1)  # definitely in the past
    item = ActionItem(
        case_id=sample_case.id,
        title="Overdue deadline",
        due_date=past_date,
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.OPEN,
    )
    db_session.add(item)
    db_session.commit()

    svc = CaseTimelineService(db_session)
    events = svc.build_payload(sample_case.id)["events"]

    assert len(events) == 1
    ev = events[0]
    assert ev.is_overdue is True
    assert ev.kind == "deadline"
    assert ev.actor == "own"


@pytest.mark.unit
def test_completed_action_item_not_overdue(db_session, sample_case):
    past_date = datetime(2024, 1, 1)
    item = ActionItem(
        case_id=sample_case.id,
        title="Done deadline",
        due_date=past_date,
        action_type=ActionItemType.DEADLINE,
        status=ActionItemStatus.COMPLETED,
    )
    db_session.add(item)
    db_session.commit()

    svc = CaseTimelineService(db_session)
    events = svc.build_payload(sample_case.id)["events"]

    assert events[0].is_overdue is False


@pytest.mark.unit
def test_quiet_gap_only_for_14_plus_days(db_session, sample_case):
    # Two docs in the same month: gap 15 days → should appear
    d1 = Document(
        case_id=sample_case.id,
        title="First",
        issued_date=datetime(2025, 3, 1),
        ingest_date=datetime.now(),
        originator_type=OriginatorType.OWN,
    )
    d2 = Document(
        case_id=sample_case.id,
        title="Second",
        issued_date=datetime(2025, 3, 16),
        ingest_date=datetime.now(),
        originator_type=OriginatorType.OWN,
    )
    # Third doc 3 days after second → no gap
    d3 = Document(
        case_id=sample_case.id,
        title="Third",
        issued_date=datetime(2025, 3, 19),
        ingest_date=datetime.now(),
        originator_type=OriginatorType.OWN,
    )
    db_session.add_all([d1, d2, d3])
    db_session.commit()

    svc = CaseTimelineService(db_session)
    payload = svc.build_payload(sample_case.id)
    gaps = payload["quiet_gaps"]

    # Find event ids
    by_title = {e.title: e for e in payload["events"]}
    assert by_title["Second"].id in gaps  # 15-day gap before second
    assert gaps[by_title["Second"].id] == 15
    assert by_title["Third"].id not in gaps  # only 3 days


@pytest.mark.unit
def test_document_type_to_kind_mapping(db_session, sample_case):
    type_kind_pairs = [
        (DocumentType.RULING, "order"),
        (DocumentType.MOTION, "filing"),
        (DocumentType.STATEMENT, "statement"),
        (DocumentType.REPORT, "report"),
        (DocumentType.RELAY, "relay"),
    ]
    for i, (doc_type, expected_kind) in enumerate(type_kind_pairs):
        doc = Document(
            case_id=sample_case.id,
            title=f"Doc {i}",
            issued_date=datetime(2025, 4, i + 1),
            ingest_date=datetime.now(),
            originator_type=OriginatorType.OWN,
            document_type=doc_type,
        )
        db_session.add(doc)
    db_session.commit()

    svc = CaseTimelineService(db_session)
    events = svc.build_payload(sample_case.id)["events"]
    kind_map = {e.title: e.kind for e in events}

    for i, (doc_type, expected_kind) in enumerate(type_kind_pairs):
        assert kind_map[f"Doc {i}"] == expected_kind, (
            f"Expected {expected_kind} for {doc_type}"
        )


@pytest.mark.unit
def test_a_copy_folds_onto_its_original(db_session, sample_case):
    from app.models.database import DocumentRelationship
    from app.models.enums import RelationshipConfidence, RelationshipType

    original = Document(
        case_id=sample_case.id,
        title="Beschluss",
        issued_date=datetime(2025, 6, 1),
        originator_type=OriginatorType.COURT,
    )
    copy = Document(
        case_id=sample_case.id,
        title="Beschluss (Anlage)",
        issued_date=datetime(2025, 6, 1),
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all([original, copy])
    db_session.flush()
    db_session.add(
        DocumentRelationship(
            from_document_id=copy.id,
            to_document_id=original.id,
            relationship_type=RelationshipType.COPY_OF,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db_session.commit()

    payload = CaseTimelineService(db_session).build_payload(sample_case.id)

    (event,) = payload["events"]
    assert event.source_document_id == original.id
    assert event.copy_count == 1


@pytest.mark.unit
def test_month_buckets_follow_the_reader_calendar(db_session, sample_case, monkeypatch):
    from zoneinfo import ZoneInfo

    from app.services import timezone_service

    monkeypatch.setattr(timezone_service, "_cached_tz", ZoneInfo("Pacific/Auckland"))
    db_session.add_all(
        [
            # 13:00 UTC on 30 Jun is already 1 Jul in Auckland.
            Document(
                case_id=sample_case.id,
                title="Instant",
                issued_date=datetime(2025, 6, 30, 13, 0),
                ingest_date=datetime(2025, 7, 1),
                originator_type=OriginatorType.COURT,
            ),
            # A printed date (00:00) is not shifted.
            Document(
                case_id=sample_case.id,
                title="Printed",
                issued_date=datetime(2025, 6, 30),
                ingest_date=datetime(2025, 7, 1),
                originator_type=OriginatorType.COURT,
            ),
        ]
    )
    db_session.commit()

    buckets = CaseTimelineService(db_session).build_payload(sample_case.id)[
        "month_buckets"
    ]
    assert [(b["key"], b["label"], b["total"]) for b in buckets] == [
        ("2025-06", "Jun 2025", 1),
        ("2025-07", "Jul 2025", 1),
    ]
