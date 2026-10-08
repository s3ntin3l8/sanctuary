"""GET /api/v1/notifications — the rail's 🔔 panel."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.timezone import now_utc
from app.main import app
from app.models.database import (
    ActionItem,
    Case,
    CaseShare,
    Document,
    IngestBatch,
    LegalCost,
)
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    CaseAccessLevel,
    CaseStatus,
    CostCategory,
    CostStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
)
from app.services import auth_service
from app.services.notifications_service import ITEMS_PER_GROUP, build_notifications

pytestmark = pytest.mark.integration

PASSWORD = "password123"  # pragma: allowlist secret


def _case(db, case_id, owner_id):
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


def _item(db, case_id, title, *, days, action_type=ActionItemType.DEADLINE, **kw):
    kw.setdefault("status", ActionItemStatus.OPEN)
    item = ActionItem(
        case_id=case_id,
        title=title,
        due_date=now_utc() + timedelta(days=days),
        action_type=action_type,
        **kw,
    )
    db.add(item)
    db.commit()
    return item


def _login(email):
    client = TestClient(app, follow_redirects=False)
    payload = {"email": email, "password": PASSWORD}
    client.post("/api/v1/auth/login", json=payload)
    return client


@pytest.fixture
def two_users(db_session):
    a = auth_service.create_user(db_session, email="a@example.com", password=PASSWORD)
    b = auth_service.create_user(db_session, email="b@example.com", password=PASSWORD)
    db_session.commit()
    return a, b


def _groups(view):
    return {g.kind: g for g in view.groups}


def test_groups_split_by_window_and_type(db_session, two_users):
    a, _ = two_users
    _case(db_session, "N-1", a.id)
    _item(db_session, "N-1", "Missed Frist", days=-2)
    _item(db_session, "N-1", "Frist this week", days=3)
    _item(db_session, "N-1", "Frist next month", days=20)
    _item(
        db_session,
        "N-1",
        "Termin",
        days=25,
        action_type=ActionItemType.COURT_DATE,
        location="Saal 3",
    )
    _item(
        db_session, "N-1", "Far Termin", days=45, action_type=ActionItemType.COURT_DATE
    )
    _item(
        db_session,
        "N-1",
        "Missed Termin",
        days=-3,
        action_type=ActionItemType.COURT_DATE,
    )
    _item(db_session, "N-1", "Opponent's Frist", days=1, addressee="opposing")
    _item(db_session, "N-1", "Done", days=-1, status=ActionItemStatus.COMPLETED)
    _item(db_session, "N-1", "Replaced", days=-1, superseded=True)

    g = _groups(build_notifications(db_session, a))
    assert [i.title for i in g["overdue_deadline"].items] == ["Missed Frist"]
    assert [i.title for i in g["upcoming_deadline"].items] == ["Frist this week"]
    # Missed hearings stay in the hearings group rather than becoming
    # "overdue deadlines"; soonest first.
    assert [i.title for i in g["hearing"].items] == ["Missed Termin", "Termin"]
    assert g["hearing"].items[1].detail == "Saal 3"
    assert g["hearing"].items[1].link == "/cases/N-1?view=review"
    assert g["hearing"].items[1].case_title == "Case N-1"


def test_pending_triage_is_the_callers_inbox_without_slicing(db_session, two_users):
    a, b = two_users
    for owner, status in (
        (a, IngestBatchStatus.PENDING),
        (a, IngestBatchStatus.AWAITING_SLICING),
        (a, IngestBatchStatus.COMPLETED),
        (b, IngestBatchStatus.PENDING),
    ):
        batch = IngestBatch(
            owner_id=owner.id,
            source_type=IngestBatchSourceType.EMAIL,
            status=status,
            case_id="_TRIAGE",
            subject=f"{owner.email} {status}",
        )
        db_session.add(batch)
        db_session.flush()
        # A bundle only exists in the triage feed if it has a document.
        db_session.add(
            Document(
                title="d",
                owner_id=owner.id,
                case_id="_TRIAGE",
                ingest_batch_id=batch.id,
            )
        )
    # ...and a document-less batch is invisible, so it must not be counted.
    db_session.add(
        IngestBatch(
            owner_id=a.id,
            source_type=IngestBatchSourceType.EMAIL,
            status=IngestBatchStatus.PENDING,
            case_id="_TRIAGE",
            subject="empty",
        )
    )
    db_session.commit()

    g = _groups(build_notifications(db_session, a))
    assert g["pending_triage"].count == 1
    assert g["pending_triage"].items[0].title == "a@example.com pending"
    assert g["pending_triage"].items[0].link == "/triage"


def test_overdue_costs_skip_settled_and_future(db_session, two_users):
    a, _ = two_users
    _case(db_session, "N-2", a.id)
    now = now_utc()
    for title, status, due in (
        ("Overdue", CostStatus.OFFEN, now - timedelta(days=1)),
        ("Paid", CostStatus.BEZAHLT, now - timedelta(days=1)),
        ("Soon", CostStatus.OFFEN, now + timedelta(days=2)),
        ("No date", CostStatus.OFFEN, None),
    ):
        db_session.add(
            LegalCost(
                case_id="N-2",
                category=CostCategory.GERICHTSKOSTEN,
                status=status,
                title=title,
                amount_net=100.0,
                amount_gross=119.0,
                amount_paid=19.0,
                due_at=due,
            )
        )
    db_session.commit()

    g = _groups(build_notifications(db_session, a))
    assert [i.title for i in g["overdue_cost"].items] == ["Overdue"]
    assert g["overdue_cost"].items[0].amount == 100.0
    assert g["overdue_cost"].items[0].link == "/costs"


def test_count_is_full_while_items_are_capped(db_session, two_users):
    a, _ = two_users
    _case(db_session, "N-3", a.id)
    for n in range(ITEMS_PER_GROUP + 2):
        _item(db_session, "N-3", f"Frist {n}", days=-1 - n)
    view = build_notifications(db_session, a)
    g = _groups(view)["overdue_deadline"]
    assert g.count == ITEMS_PER_GROUP + 2
    assert len(g.items) == ITEMS_PER_GROUP
    # Soonest first, so the most-recently-missed deadline leads.
    assert g.items[0].title == "Frist 6"
    assert view.total == ITEMS_PER_GROUP + 2


def test_scoped_to_visible_cases(auth_enabled, db_session, two_users):
    a, b = two_users
    _case(db_session, "N-A", a.id)
    _case(db_session, "N-B", b.id)
    _case(db_session, "N-S", b.id)
    db_session.add(
        CaseShare(case_id="N-S", user_id=a.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.commit()
    _item(db_session, "N-A", "Own", days=-1)
    _item(db_session, "N-B", "Theirs", days=-1)
    _item(db_session, "N-S", "Shared", days=-1)

    body = _login("a@example.com").get("/api/v1/notifications").json()
    overdue = next(g for g in body["groups"] if g["kind"] == "overdue_deadline")
    assert sorted(i["title"] for i in overdue["items"]) == ["Own", "Shared"]
    assert body["total"] == 2

    anonymous = TestClient(app, follow_redirects=False)
    assert anonymous.get("/api/v1/notifications").status_code == 401


def test_admin_sees_every_case(db_session, two_users):
    a, b = two_users
    _case(db_session, "N-X", a.id)
    _case(db_session, "N-Y", b.id)
    _item(db_session, "N-X", "A's Frist", days=-1)
    _item(db_session, "N-Y", "B's Frist", days=-1)
    admin = auth_service.get_user_by_email(db_session, "admin@localhost")

    g = _groups(build_notifications(db_session, admin))
    assert sorted(i.title for i in g["overdue_deadline"].items) == [
        "A's Frist",
        "B's Frist",
    ]
    assert _groups(build_notifications(db_session, a))["overdue_deadline"].count == 1
