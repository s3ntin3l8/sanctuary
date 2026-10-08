"""Notify mode's background check, the mode fan-out, and the sync-mode migration."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models.database import GmailMessageIndex, IngestBatch, UserSettings
from app.services import gmail_index_service, user_settings_service
from app.services.ingestion.gmail import (
    GmailConnection,
    GmailReconnectRequired,
    parse_metadata,
)
from app.tasks import gmail_sync

pytestmark = pytest.mark.unit

SYNC_POINT = "2024-01-10T00:00:00+00:00"


@pytest.fixture
def gmail_user(db_session):
    from app.services import auth_service

    user = auth_service.create_user(
        db_session,
        email="checker@example.com",
        password="password123",  # pragma: allowlist secret
    )
    user_settings_service.set_gmail_credentials(
        db_session, user.id, credentials_json="{}", connected_at="2024-01-01"
    )
    user_settings_service.set_gmail_inbox_filters(
        db_session, user.id, allowlist=["lawyer@example.com"], label_filter=""
    )
    user_settings_service.set_gmail_sync_point(db_session, user.id, SYNC_POINT)
    db_session.commit()
    return user


def _raw(gmail_id, subject="8372/25 x"):
    return {
        "id": gmail_id,
        "threadId": f"t-{gmail_id}",
        "internalDate": str(int(datetime(2024, 1, 12, tzinfo=UTC).timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "lawyer@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Fri, 05 Jan 2024 10:00:00 +0000"},
                {"name": "Message-ID", "value": f"<{gmail_id}@x>"},
            ],
        },
    }


def _settings(db, user_id):
    db.expire_all()
    return db.query(UserSettings).filter_by(user_id=user_id).one().settings_json


def _run_check(user_id, ids, raws, *, acquired=True):
    with (
        patch(
            "app.services.ingestion.gmail.get_gmail_service",
            return_value=GmailConnection(MagicMock(), None),
        ),
        patch(
            "app.tasks.gmail_sync.list_message_ids", return_value=iter(ids)
        ) as listed,
        patch("app.tasks.gmail_sync.fetch_metadata", return_value=raws) as fetched,
        patch("app.tasks.gmail_sync.ingest_raw_email") as ingest,
        patch("app.tasks.gmail_sync.fetch_raw_message") as raw_fetch,
        patch("app.tasks.gmail_sync._user_sync_lock") as lock,
    ):
        lock.return_value.__enter__.return_value = acquired
        result = gmail_sync.check_gmail_new.run(user_id)
    return result, listed, fetched, ingest, raw_fetch


# --- check_gmail_new ---------------------------------------------------------


def test_check_indexes_new_headers_and_never_ingests_or_downloads_mail(
    gmail_user, db_session
):
    result, listed, _, ingest, raw_fetch = _run_check(
        gmail_user.id, ["g1", "g2"], [_raw("g1"), _raw("g2")]
    )

    assert "2 new" in result
    ingest.assert_not_called()
    raw_fetch.assert_not_called()  # headers only: no message body is downloaded
    rows = db_session.query(GmailMessageIndex).filter_by(owner_id=gmail_user.id).all()
    assert {r.gmail_id for r in rows} == {"g1", "g2"}
    assert all(r.received_at is not None for r in rows)
    assert db_session.query(IngestBatch).count() == 0
    assert _settings(db_session, gmail_user.id)["gmail_last_check_at"]


def test_check_only_asks_gmail_for_mail_after_the_sync_point(gmail_user):
    _, listed, *_ = _run_check(gmail_user.id, [], [])
    query = listed.call_args.args[1]
    after = int(datetime.fromisoformat(SYNC_POINT).timestamp())
    assert f"after:{after}" in query and "from:lawyer@example.com" in query


def test_check_does_not_refetch_what_is_already_indexed(gmail_user, db_session):
    gmail_index_service.upsert_metadata(
        db_session, gmail_user.id, [parse_metadata(_raw("g1"))]
    )
    db_session.commit()
    _, _, fetched, *_ = _run_check(gmail_user.id, ["g1", "g2"], [_raw("g2")])
    assert fetched.call_args.args[1] == ["g2"]


def test_check_skips_when_the_mailbox_is_busy(gmail_user):
    result, listed, *_ = _run_check(gmail_user.id, ["g1"], [_raw("g1")], acquired=False)
    assert result == "Busy"
    listed.assert_not_called()


def test_check_without_a_sync_point_anchors_one_instead_of_querying(
    gmail_user, db_session
):
    data = {
        k: v
        for k, v in _settings(db_session, gmail_user.id).items()
        if k != "gmail_last_sync_at"
    }
    db_session.query(UserSettings).filter_by(
        user_id=gmail_user.id
    ).one().settings_json = data
    db_session.commit()

    result, listed, *_ = _run_check(gmail_user.id, ["g1"], [_raw("g1")])

    assert result == "Initialized sync watermark"
    listed.assert_not_called()
    assert _settings(db_session, gmail_user.id)["gmail_last_sync_at"]


def test_check_without_any_filter_does_nothing(gmail_user, db_session):
    user_settings_service.set_gmail_inbox_filters(
        db_session, gmail_user.id, allowlist=[], label_filter=""
    )
    db_session.commit()
    result, listed, *_ = _run_check(gmail_user.id, ["g1"], [_raw("g1")])
    assert result == "No sender allowlist or label set"
    listed.assert_not_called()


def test_check_needing_a_reconnect_is_recorded_not_retried(gmail_user, db_session):
    with (
        patch(
            "app.services.ingestion.gmail.get_gmail_service",
            side_effect=GmailReconnectRequired("revoked"),
        ),
        patch("app.tasks.gmail_sync._user_sync_lock") as lock,
    ):
        lock.return_value.__enter__.return_value = True
        result = gmail_sync.check_gmail_new.run(gmail_user.id)
    assert result == "Reconnect required"
    sj = _settings(db_session, gmail_user.id)
    assert (
        sj["gmail_reconnect_required"] is True
        and "revoked" in sj["gmail_last_sync_error"]
    )


def test_a_successful_check_clears_a_stale_error(gmail_user, db_session):
    user_settings_service.record_gmail_sync_outcome(
        db_session, gmail_user.id, error="boom", reconnect_required=True
    )
    db_session.commit()
    _run_check(gmail_user.id, [], [])
    sj = _settings(db_session, gmail_user.id)
    assert (
        sj["gmail_last_sync_error"] is None and sj["gmail_reconnect_required"] is False
    )


# --- Beat fan-out by mode ----------------------------------------------------


def test_beat_dispatches_a_check_for_notify_and_a_sync_for_auto(db_session):
    with (
        patch.object(
            user_settings_service,
            "gmail_users_by_mode",
            return_value={"notify": [1, 2], "auto": [3]},
        ),
        patch("app.tasks.dispatch.dispatch_task") as dispatch,
    ):
        gmail_sync.sync_gmail_incremental()
    calls = {(c.args[0], c.args[1]) for c in dispatch.call_args_list}
    assert calls == {
        (gmail_sync.sync_gmail_for_user, 3),
        (gmail_sync.check_gmail_new, 1),
        (gmail_sync.check_gmail_new, 2),
    }


# --- parse_metadata ----------------------------------------------------------


def test_parse_metadata_keeps_gmails_receipt_time():
    meta = parse_metadata(_raw("g1"))
    assert meta["received_at"] == datetime(2024, 1, 12, tzinfo=UTC)
    assert meta["sent_at"] == datetime(2024, 1, 5, 10, tzinfo=UTC)  # the Date header


# --- The sync-mode data migration --------------------------------------------


def _migration():
    path = next(Path(__file__).parents[2].glob("alembic/versions/b8d2f4a6c0e1_*.py"))
    spec = importlib.util.spec_from_file_location("sync_mode_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "before,after",
    [
        # Connected, auto-sync on: stays automatic.
        (
            {"gmail_credentials_json": "x", "gmail_auto_sync": True},
            {"gmail_credentials_json": "x", "gmail_sync_mode": "auto"},
        ),
        # Connected, off (the old default): moves to notify.
        (
            {"gmail_credentials_json": "x", "gmail_auto_sync": False},
            {"gmail_credentials_json": "x", "gmail_sync_mode": "notify"},
        ),
        # Connected and the flag was never written.
        (
            {"gmail_credentials_json": "x"},
            {"gmail_credentials_json": "x", "gmail_sync_mode": "notify"},
        ),
        # Not connected: only the old flag goes.
        ({"gmail_auto_sync": True, "other": 1}, {"other": 1}),
    ],
)
def test_migration_maps_the_old_flag_to_a_mode(before, after):
    assert _migration()._to_mode(before) == after


def test_migration_leaves_unrelated_settings_alone():
    assert _migration()._to_mode({"theme": "dark"}) is None


def test_migration_downgrade_maps_back():
    flag = _migration()._to_flag
    assert flag({"gmail_sync_mode": "auto"}) == {"gmail_auto_sync": True}
    assert flag({"gmail_sync_mode": "notify"}) == {"gmail_auto_sync": False}
    assert flag({"gmail_sync_mode": "off", "x": 1}) == {
        "x": 1,
        "gmail_auto_sync": False,
    }
    assert flag({"x": 1}) is None
