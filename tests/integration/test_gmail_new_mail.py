"""Notify mode: mail that arrived after the sync point, offered for import."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import IngestBatch, User, UserSettings
from app.models.enums import IngestBatchSourceType
from app.services import gmail_index_service, gmail_runs, user_settings_service

pytestmark = pytest.mark.integration

client = TestClient(app)

CREDS = json.dumps({"token": "t", "refresh_token": "r"})  # pragma: allowlist secret
SYNC_POINT = "2024-01-10T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _runs(fake_run_state):
    return fake_run_state


def _admin(db) -> int:
    return db.query(User).filter_by(email="admin@localhost").one().id


def _connect(db, uid, *, sync_point=SYNC_POINT, label_filter=""):
    user_settings_service.set_gmail_credentials(
        db, uid, credentials_json=CREDS, connected_at="2024-01-01T00:00:00+00:00"
    )
    user_settings_service.set_gmail_inbox_filters(
        db, uid, allowlist=["lawyer@example.com"], label_filter=label_filter
    )
    if sync_point:
        user_settings_service.set_gmail_sync_point(db, uid, sync_point)
    db.commit()


def _index(db, uid, gmail_id, day, *, received=None, subject="8372/25 x"):
    """Index a message sent on Jan ``day``; ``received`` defaults to the same day."""
    arrived = received if received is not None else day
    gmail_index_service.upsert_metadata(
        db,
        uid,
        [
            {
                "gmail_id": gmail_id,
                "thread_id": f"t-{gmail_id}",
                "message_id": f"<{gmail_id}@x>",
                "sender": "lawyer@example.com",
                "subject": subject,
                "sent_at": datetime(2024, 1, day, tzinfo=UTC),
                "received_at": datetime(2024, 1, arrived, tzinfo=UTC)
                if arrived
                else None,
                "has_attachments": False,
                "size_estimate": 1,
            }
        ],
    )
    db.commit()


def _ingest(db, uid, gmail_id):
    db.add(
        IngestBatch(
            source_type=IngestBatchSourceType.EMAIL,
            subject="x",
            message_id=f"<{gmail_id}@x>",
            owner_id=uid,
        )
    )
    db.commit()


def _sj(db, uid):
    db.expire_all()
    return db.query(UserSettings).filter_by(user_id=uid).one().settings_json


def _new():
    return client.get("/api/v1/gmail/new").json()


# --- Listing -----------------------------------------------------------------


def test_new_is_mail_received_after_the_sync_point_and_not_imported(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _index(db_session, uid, "old", 5)  # before the sync point
    _index(db_session, uid, "n2", 12)
    _index(db_session, uid, "n1", 11)
    _index(db_session, uid, "done", 13)
    _ingest(db_session, uid, "done")

    body = _new()

    assert body["count"] == 2 and body["sync_mode"] == "notify"
    assert [m["gmail_id"] for m in body["items"]] == ["n1", "n2"]  # oldest first
    assert body["since"].startswith("2024-01-10")


def test_new_goes_by_when_gmail_received_it_not_the_date_header(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    # Date header says Jan 5, but it landed in the mailbox on Jan 12.
    _index(db_session, uid, "late", 5, received=12)
    # Date header is recent, but it was received before the sync point.
    _index(db_session, uid, "early", 12, received=3)

    assert [m["gmail_id"] for m in _new()["items"]] == ["late"]


def test_rows_indexed_before_received_at_existed_fall_back_to_the_date(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _index(db_session, uid, "legacy", 12, received=0)  # received_at stored as NULL
    assert [m["gmail_id"] for m in _new()["items"]] == ["legacy"]


def test_the_list_is_capped_but_the_count_is_not(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid, sync_point="2023-12-31T00:00:00+00:00")
    for i in range(105):
        gmail_index_service.upsert_metadata(
            db_session,
            uid,
            [
                {
                    "gmail_id": f"m{i:03d}",
                    "thread_id": f"t{i}",
                    "message_id": f"<m{i}@x>",
                    "sender": "lawyer@example.com",
                    "subject": "x",
                    "sent_at": datetime(2024, 1, 1, tzinfo=UTC),
                    "received_at": datetime(2024, 1, 2, 0, i % 60, tzinfo=UTC),
                    "has_attachments": False,
                    "size_estimate": 1,
                }
            ],
        )
    db_session.commit()
    body = _new()
    assert body["count"] == 105 and len(body["items"]) == 100


def test_nothing_is_new_without_a_connection_or_sync_point(db_session):
    assert _new() == {
        "count": 0,
        "sync_mode": "off",
        "since": None,
        "checked_at": None,
        "items": [],
    }
    uid = _admin(db_session)
    _connect(db_session, uid, sync_point=None)
    db_session.query(UserSettings).filter_by(user_id=uid).one().settings_json = {
        k: v for k, v in _sj(db_session, uid).items() if k != "gmail_last_sync_at"
    }
    db_session.commit()
    assert _new()["count"] == 0


def test_new_mail_is_per_user(db_session):
    uid = _admin(db_session)
    from app.services import auth_service

    other = auth_service.create_user(
        db_session,
        email="other@example.com",
        password="password123",  # pragma: allowlist secret
    )
    _connect(db_session, other.id)
    _index(db_session, other.id, "theirs", 12)
    _connect(db_session, uid)
    assert _new()["count"] == 0


# --- Check now ---------------------------------------------------------------


def test_check_now_queues_a_check_and_needs_a_ready_mailbox(db_session):
    assert (
        client.post("/api/v1/gmail/new/check").json()["code"] == "gmail_not_connected"
    )
    uid = _admin(db_session)
    _connect(db_session, uid)
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        assert client.post("/api/v1/gmail/new/check").status_code == 202
    assert dispatch.call_args.args[1] == uid


# --- Import ------------------------------------------------------------------


def test_importing_the_new_mail_is_oldest_first_and_skips_the_rest(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _index(db_session, uid, "old", 5)
    _index(db_session, uid, "n2", 12)
    _index(db_session, uid, "n1", 11)
    _index(db_session, uid, "done", 13)
    _ingest(db_session, uid, "done")

    with patch("app.tasks.dispatch.dispatch_task"):
        resp = client.post("/api/v1/gmail/import", json={"new": True})

    assert resp.status_code == 202 and resp.json() == {"queued": 2}
    assert gmail_runs.get_run("import", uid)["remaining"] == ["n1", "n2"]


def test_importing_new_mail_can_be_narrowed_to_a_pick(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _index(db_session, uid, "n1", 11)
    _index(db_session, uid, "n2", 12)
    with patch("app.tasks.dispatch.dispatch_task"):
        client.post("/api/v1/gmail/import", json={"new": True, "gmail_ids": ["n2"]})
    assert gmail_runs.get_run("import", uid)["remaining"] == ["n2"]


# --- Dismiss -----------------------------------------------------------------


def test_dismiss_moves_the_sync_point_to_the_newest_listed_mail(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    user_settings_service._update_gmail(db_session, uid, gmail_failed_message_ids=["f"])
    _index(db_session, uid, "n1", 11)
    _index(db_session, uid, "n2", 12)
    db_session.commit()

    body = client.post("/api/v1/gmail/new/dismiss").json()

    assert body["count"] == 0
    sj = _sj(db_session, uid)
    assert sj["gmail_last_sync_at"].startswith("2024-01-12")
    assert sj["gmail_failed_message_ids"] == ["f"]  # not forgotten, unlike a reset
    # The skipped mail is still importable from the history.
    with patch("app.tasks.dispatch.dispatch_task"):
        client.post("/api/v1/gmail/import", json={"gmail_ids": ["n1"]})
    assert gmail_runs.get_run("import", uid)["remaining"] == ["n1"]


def test_mail_that_arrives_after_a_dismiss_is_new_again(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    _index(db_session, uid, "n1", 11)
    client.post("/api/v1/gmail/new/dismiss")
    _index(db_session, uid, "n2", 14)
    assert [m["gmail_id"] for m in _new()["items"]] == ["n2"]


def test_dismissing_with_nothing_new_changes_nothing(db_session):
    uid = _admin(db_session)
    _connect(db_session, uid)
    client.post("/api/v1/gmail/new/dismiss")
    assert _sj(db_session, uid)["gmail_last_sync_at"] == SYNC_POINT
