"""Round 2 regression: action items are only authoritative when set BY a
court IN a direct court document. Mirrors the Streitwert gate exactly.

ib-0033 doc #98 case: an opposing-party motion (originator=OPPOSING,
court_relay=True) had an action item materialise from a Frist quoted inside
an appendix Verfügung. Quoting != setting. The call-site gate in
`document_enricher.py` (Phase 3 write) refuses to call `create_from_payload`
on non-court sources and erases any stale items from prior runs.
"""

from datetime import datetime

import pytest

from app.models.database import ActionItem, Document
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    OriginatorType,
    SignificanceTier,
)
from app.services.intelligence.action_items import (
    create_from_payload,
    purge_action_items_from_doc,
)


def _make_doc(
    db,
    case,
    *,
    originator_type: OriginatorType,
    court_relay: bool = False,
) -> Document:
    doc = Document(
        title="Test doc",
        content="content",
        case_id=case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=originator_type,
        court_relay=court_relay,
        issued_date=datetime(2025, 5, 1),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _seed_item(
    db, doc, *, due: datetime, action_type: ActionItemType, superseded: bool = False
):
    item = ActionItem(
        case_id=doc.case_id,
        source_document_id=doc.id,
        title="seed",
        due_date=due,
        action_type=action_type,
        status=(ActionItemStatus.DISMISSED if superseded else ActionItemStatus.OPEN),
        superseded=superseded,
        ingest_date=datetime.now(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# --- purge_action_items_from_doc -------------------------------------------


@pytest.mark.unit
def test_purge_action_items_deletes_only_non_superseded(db_session, sample_case):
    """Mirrors the existing cleanup contract in create_from_payload — tombstones
    are permanent guards and must survive."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.OPPOSING)
    live = _seed_item(
        db_session, doc, due=datetime(2025, 7, 18), action_type=ActionItemType.DEADLINE
    )
    tombstone = _seed_item(
        db_session,
        doc,
        due=datetime(2025, 9, 15),
        action_type=ActionItemType.COURT_DATE,
        superseded=True,
    )
    # Capture IDs before the purge — after delete + commit, accessing
    # `live.id` triggers a refresh that fails with ObjectDeletedError.
    live_id = live.id
    tombstone_id = tombstone.id

    deleted = purge_action_items_from_doc(doc.id, db_session)
    db_session.commit()
    db_session.expire_all()

    assert deleted == 1
    assert db_session.get(ActionItem, live_id) is None
    assert db_session.get(ActionItem, tombstone_id) is not None


@pytest.mark.unit
def test_purge_action_items_doc_with_no_items_is_noop(db_session, sample_case):
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)
    deleted = purge_action_items_from_doc(doc.id, db_session)
    assert deleted == 0


# --- Gate semantics — exercised via direct create_from_payload + purge -------
# The actual gate (originator + court_relay decision) lives at the call site in
# document_enricher.py; here we verify the two primitives the gate composes.


@pytest.mark.unit
def test_create_from_payload_still_works_for_court_doc(db_session, sample_case):
    """Direct contract regression — the function isn't gated internally; the
    call-site gate is responsible. When the call site allows the call, items
    materialise normally."""
    doc = _make_doc(db_session, sample_case, originator_type=OriginatorType.COURT)
    count = create_from_payload(
        case_id=doc.case_id,
        source_doc_id=doc.id,
        proceeding_id=None,
        actions=[
            {
                "title": "Stellungnahme",
                "action_type": "deadline",
                "due_date": "2025-07-18",
            }
        ],
        db=db_session,
    )
    db_session.commit()

    assert count == 1
    rows = (
        db_session.query(ActionItem)
        .filter(ActionItem.source_document_id == doc.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].title == "Stellungnahme"


@pytest.mark.unit
def test_purge_erases_doc_98_pattern(db_session, sample_case):
    """ib-0033 doc #98 reenactment: a non-court source has a stale OPEN
    action item from a prior run. The call-site gate uses purge instead of
    create_from_payload; the stale item disappears."""
    # Doc is opposing — the call-site gate would refuse create_from_payload.
    doc = _make_doc(
        db_session,
        sample_case,
        originator_type=OriginatorType.OPPOSING,
        court_relay=True,
    )
    stale = _seed_item(
        db_session,
        doc,
        due=datetime(2025, 7, 18),
        action_type=ActionItemType.DEADLINE,
    )
    stale_id = stale.id

    purged = purge_action_items_from_doc(doc.id, db_session)
    db_session.commit()
    db_session.expire_all()

    assert purged == 1
    assert db_session.get(ActionItem, stale_id) is None
