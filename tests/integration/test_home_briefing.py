"""GET/POST /api/v1/home/briefing — the per-user, per-day morning briefing."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import ActionItem, Case, HomeBriefing
from app.models.enums import ActionItemType, CaseStatus, Jurisdiction
from app.services import auth_service
from app.services.intelligence import home_briefing_generator as gen
from app.services.intelligence.schemas import HomeBriefingOut

pytestmark = pytest.mark.integration

PASSWORD = "password123"  # pragma: allowlist secret

client = TestClient(app)


def _admin(db):
    return auth_service.get_user_by_email(db, "admin@localhost")


def test_first_request_claims_and_dispatches_once(db_session, mock_dispatch_task):
    first = client.get("/api/v1/home/briefing")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "processing"
    assert body["summary"] is None
    assert body["day"] == gen.today_for_user().isoformat()
    assert mock_dispatch_task.call_count == 1
    _, user_id, day_iso = mock_dispatch_task.call_args.args
    assert user_id == _admin(db_session).id
    assert day_iso == body["day"]

    # Polling while the run is in flight neither re-claims nor re-dispatches.
    assert client.get("/api/v1/home/briefing").json()["status"] == "processing"
    assert client.post("/api/v1/home/briefing/refresh").status_code == 202
    assert mock_dispatch_task.call_count == 1

    row = db_session.query(HomeBriefing).filter_by(user_id=user_id).one()
    assert row.queued_at is not None


def test_generate_writes_the_row_and_refresh_rearms(db_session, mock_dispatch_task):
    admin = _admin(db_session)
    db_session.add(
        Case(
            id="BRF-1",
            title="Weber ./. Weber",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
            owner_id=admin.id,
        )
    )
    db_session.add(
        ActionItem(
            case_id="BRF-1",
            title="Counter-statement",
            due_date=datetime.now(UTC) + timedelta(days=1),
            action_type=ActionItemType.DEADLINE,
        )
    )
    db_session.commit()
    day = gen.today_for_user()
    assert gen.claim_for_dispatch(db_session, admin.id, day) is True
    assert gen.claim_for_dispatch(db_session, admin.id, day) is False  # in flight

    captured: dict = {}

    def fake_ai(**kw):
        captured.update(kw)
        return HomeBriefingOut(
            summary="One deadline lands tomorrow in BRF-1.",
            priorities=["BRF-1: file the counter-statement", " ", "Triage: nothing"],
        )

    with patch.object(gen, "call_json_ai", side_effect=fake_ai):
        gen.generate(admin.id, day)
    gen.release_claim(db_session, admin.id, day)

    assert captured["schema"] is HomeBriefingOut
    assert "Counter-statement [deadline] case BRF-1" in captured["user_prompt"]
    assert "(1d)" in captured["user_prompt"]

    body = client.get("/api/v1/home/briefing").json()
    assert body["status"] == "ready"
    assert body["summary"] == "One deadline lands tomorrow in BRF-1."
    assert body["priorities"] == [
        "BRF-1: file the counter-statement",
        "Triage: nothing",
    ]
    assert body["generated_at"] is not None
    assert mock_dispatch_task.call_count == 0

    # Refresh re-arms the finished row and dispatches again.
    assert client.post("/api/v1/home/briefing/refresh").json()["status"] == "processing"
    assert mock_dispatch_task.call_count == 1


def test_failed_run_is_reported_and_can_be_retried(db_session, mock_dispatch_task):
    admin = _admin(db_session)
    day = gen.today_for_user()
    assert gen.claim_for_dispatch(db_session, admin.id, day)
    gen.mark_failed(admin.id, day, "provider unreachable: boom")
    gen.release_claim(db_session, admin.id, day)

    body = client.get("/api/v1/home/briefing").json()
    assert body["status"] == "failed"
    assert body["error"] == "provider unreachable: boom"
    assert mock_dispatch_task.call_count == 0
    assert client.post("/api/v1/home/briefing/refresh").json()["status"] == "processing"
    assert mock_dispatch_task.call_count == 1


def test_briefings_are_per_user(auth_enabled, db_session, mock_dispatch_task):
    a = auth_service.create_user(db_session, email="a@example.com", password=PASSWORD)
    b = auth_service.create_user(db_session, email="b@example.com", password=PASSWORD)
    db_session.commit()
    day = gen.today_for_user()
    db_session.add(
        HomeBriefing(
            user_id=a.id, day=day, status="ready", summary="A's day", priorities=[]
        )
    )
    db_session.commit()

    def login(email):
        c = TestClient(app, follow_redirects=False)
        c.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        return c

    assert (
        login("a@example.com").get("/api/v1/home/briefing").json()["summary"]
        == "A's day"
    )
    other = login("b@example.com").get("/api/v1/home/briefing").json()
    assert other["status"] == "processing"
    assert other["summary"] is None
    assert mock_dispatch_task.call_args.args[1] == b.id
    anonymous = TestClient(app, follow_redirects=False)
    assert anonymous.get("/api/v1/home/briefing").status_code == 401
