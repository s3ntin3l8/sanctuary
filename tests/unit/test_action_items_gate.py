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


# --- relays and exhibits through enrich() -----------------------------------


def _enrich(db_session, doc, result):
    from unittest.mock import patch

    from app.services.intelligence.document_enricher import enrich

    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.document_enricher._call_enricher_sync",
            return_value=result,
        ),
    ):
        enrich(doc.id)
    db_session.expire_all()


def _items(db_session, doc):
    return (
        db_session.query(ActionItem)
        .filter(ActionItem.source_document_id == doc.id)
        .all()
    )


def _action(title, action_type, due, addressee):
    return {
        "title": title,
        "action_type": action_type,
        "due_date": due,
        "addressee": addressee,
        "confidence": "high",
    }


def _court_doc(db, case, *, relay=False, **kw):
    doc = Document(
        title="Court letter",
        content="Das Gericht ... " * 20,
        case_id=case.id,
        significance_tier=SignificanceTier.SIGNIFICANT,
        originator_type=OriginatorType.COURT,
        sender="Amtsgericht Ingolstadt",
        court_relay=relay,
        issued_date=datetime(2026, 9, 1),
        **kw,
    )
    db.add(doc)
    db.commit()
    return doc


@pytest.mark.unit
def test_relay_keeps_the_courts_instruction_but_not_the_forwarded_demands(
    db_session, sample_case
):
    relay = _court_doc(db_session, sample_case, relay=True)

    _enrich(
        db_session,
        relay,
        {
            "document_type": "relay",
            "court_relay": True,
            "action_items": [
                _action("Stellungnahme", "response_required", "2026-09-15", "user"),
                _action("Attest vorlegen", "deadline", "2026-09-20", "opposing"),
                _action("Gebühr zahlen", "payment_due", "2026-09-25", "user"),
            ],
        },
    )

    assert [i.title for i in _items(db_session, relay)] == ["Stellungnahme"]


@pytest.mark.unit
def test_an_exhibit_under_a_party_filing_sets_no_action_items(db_session, sample_case):
    """A court letter copied into the opposing side's motion is evidence: its
    old Frist is not a deadline for us."""
    from app.models.enums import DocumentRole

    motion = Document(
        title="Antrag",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.OPPOSING,
        role=DocumentRole.COVER_LETTER,
    )
    db_session.add(motion)
    db_session.flush()
    exhibit = _court_doc(
        db_session, sample_case, parent_id=motion.id, role=DocumentRole.ENCLOSURE
    )

    _enrich(
        db_session,
        exhibit,
        {
            "document_type": "correspondence",
            "action_items": [
                _action("Attest einreichen", "deadline", "2026-09-20", "user")
            ],
        },
    )

    assert _items(db_session, exhibit) == []


@pytest.mark.unit
def test_a_ruling_a_court_relays_is_not_an_exhibit(db_session, sample_case):
    from app.models.enums import DocumentRole

    relay = _court_doc(
        db_session, sample_case, relay=True, role=DocumentRole.COVER_LETTER
    )
    ruling = _court_doc(
        db_session, sample_case, parent_id=relay.id, role=DocumentRole.ENCLOSURE
    )

    _enrich(
        db_session,
        ruling,
        {
            "document_type": "ruling",
            "action_items": [
                _action("Beschwerde einlegen", "deadline", "2026-09-20", "user")
            ],
        },
    )

    assert [i.title for i in _items(db_session, ruling)] == ["Beschwerde einlegen"]


@pytest.mark.unit
def test_a_court_letter_forwarded_in_the_users_own_mail_keeps_its_deadline(
    db_session, sample_case
):
    """The user's mail to their lawyer is transport, not a party filing: the
    court's Frist on the letter they attach is still a deadline for us."""
    from app.models.enums import DocumentRole

    body = Document(
        title="Anweisung",
        content="x",
        case_id=sample_case.id,
        originator_type=OriginatorType.OWN,
        role=DocumentRole.COVER_LETTER,
        original_filename="email_body_41.txt",
    )
    db_session.add(body)
    db_session.flush()
    letter = _court_doc(
        db_session,
        sample_case,
        relay=True,
        parent_id=body.id,
        role=DocumentRole.ENCLOSURE,
    )

    _enrich(
        db_session,
        letter,
        {
            "document_type": "relay",
            "court_relay": True,
            "action_items": [
                _action("Stellungnahme", "response_required", "2026-09-15", "user")
            ],
        },
    )

    assert [i.title for i in _items(db_session, letter)] == ["Stellungnahme"]


@pytest.mark.unit
def test_a_relay_summary_is_one_line_and_other_documents_keep_three(
    db_session, sample_case
):
    summary = {
        "legal_significance": "Forwards the Antrag of the opposing side.",
        "required_action": "Stellungnahme binnen 2 Wochen",
        "financial_impact": "Keine direkten Kosten.",
    }
    relay = _court_doc(db_session, sample_case, relay=True)
    plain = _court_doc(db_session, sample_case)

    _enrich(
        db_session,
        relay,
        {"document_type": "relay", "court_relay": True, "management_summary": summary},
    )
    _enrich(
        db_session,
        plain,
        {
            "document_type": "ruling",
            "court_relay": False,
            "management_summary": summary,
        },
    )

    assert relay.ai_summary == {
        "legal_significance": None,
        "required_action": "Stellungnahme binnen 2 Wochen",
        "financial_impact": None,
    }
    assert plain.ai_summary == summary
