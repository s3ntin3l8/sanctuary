"""index_gmail_mailbox and import_gmail_messages (the history import tasks)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.database import GmailMessageIndex, UserSettings
from app.services import gmail_index_service, gmail_runs, user_settings_service
from app.services.ingestion.gmail import GmailConnection, GmailReconnectRequired
from app.tasks import gmail_sync

pytestmark = pytest.mark.unit


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.fixture(autouse=True)
def fake_runs():
    with patch.object(gmail_runs, "_get_client", return_value=_FakeRedis()):
        yield


@pytest.fixture
def gmail_user(db_session):
    from app.services import auth_service

    user = auth_service.create_user(
        db_session,
        email="importer@example.com",
        password="password123",  # pragma: allowlist secret
    )
    user_settings_service.set_gmail_credentials(
        db_session, user.id, credentials_json="{}", connected_at="2026-01-01"
    )
    user_settings_service.set_gmail_inbox_filters(
        db_session, user.id, allowlist=["lawyer@example.com"], label_filter="Sanctuary"
    )
    db_session.commit()
    return user


def _raw(gmail_id, subject, day, thread=None):
    return {
        "id": gmail_id,
        "threadId": thread or f"t-{gmail_id}",
        "internalDate": "0",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "lawyer@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": f"Mon, {day:02d} Jan 2024 10:00:00 +0000"},
                {"name": "Message-ID", "value": f"<{gmail_id}@x>"},
            ],
        },
    }


def _locked():
    lock = patch("app.tasks.gmail_sync._user_sync_lock")
    return lock


def _settings(db, user_id):
    db.expire_all()
    return db.query(UserSettings).filter_by(user_id=user_id).one().settings_json


# --- index_gmail_mailbox -----------------------------------------------------


def _run_index(user_id, ids, metadata):
    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(MagicMock(), None),
        ),
        patch(
            "app.tasks.gmail_sync.list_message_ids", return_value=iter(ids)
        ) as listed,
        patch("app.tasks.gmail_sync.fetch_metadata", side_effect=metadata) as fetched,
        _locked() as lock,
    ):
        lock.return_value.__enter__.return_value = True
        gmail_runs.begin_run("index", user_id, {"total": 0, "done": 0})
        result = gmail_sync.index_gmail_mailbox.run(user_id)
    return result, listed, fetched


def test_index_stores_headers_groups_them_and_reports_progress(gmail_user, db_session):
    raws = [
        _raw("g1", "8372/25 Schriftsatz", 1, thread="T"),
        _raw("g2", "AW: Ihr Schreiben", 3, thread="T"),
    ]
    result, listed, _ = _run_index(gmail_user.id, ["g1", "g2"], [raws])

    assert "Indexed 2" in result
    # Every message of the allowlisted senders, all time — and the label filter applies.
    query = listed.call_args.args[1]
    assert "from:lawyer@example.com" in query and "label:Sanctuary" in query
    assert "after:" not in query
    rows = {r.gmail_id: r for r in db_session.query(GmailMessageIndex)}
    assert rows["g1"].internal_id == "8372-25"
    assert (
        rows["g2"].internal_id is None and rows["g2"].group_key == "8372-25"
    )  # thread
    state = gmail_runs.get_run("index", gmail_user.id)
    assert state["finished_at"] and (state["done"], state["total"]) == (2, 2)


def test_index_refresh_only_fetches_ids_it_does_not_have(gmail_user, db_session):
    _run_index(
        gmail_user.id,
        ["g1", "g2"],
        [[_raw("g1", "8372/25 a", 1), _raw("g2", "8372/25 b", 2)]],
    )
    _, _, fetched = _run_index(
        gmail_user.id, ["g3", "g1", "g2"], [[_raw("g3", "8372/25 c", 3)]]
    )

    assert fetched.call_args.args[1] == ["g3"]
    assert db_session.query(GmailMessageIndex).count() == 3


def test_index_skips_a_message_it_cannot_parse(gmail_user, db_session):
    broken = {"id": "bad", "payload": {"headers": []}}  # no Date and no internalDate
    result, _, _ = _run_index(
        gmail_user.id, ["g1", "bad"], [[_raw("g1", "x", 1), broken]]
    )
    assert db_session.query(GmailMessageIndex).count() == 1
    assert "Indexed 2" in result


def test_index_without_an_allowlist_explains_itself(gmail_user, db_session):
    user_settings_service.set_gmail_inbox_filters(
        db_session, gmail_user.id, allowlist=[], label_filter=""
    )
    db_session.commit()
    result, listed, _ = _run_index(gmail_user.id, [], [])
    assert result == "Not configured"
    listed.assert_not_called()
    assert "allowlist" in gmail_runs.get_run("index", gmail_user.id)["error"]


def test_index_needing_a_reconnect_is_recorded_not_retried(gmail_user, db_session):
    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            side_effect=GmailReconnectRequired("revoked"),
        ),
        _locked() as lock,
    ):
        lock.return_value.__enter__.return_value = True
        gmail_runs.begin_run("index", gmail_user.id, {"total": 0, "done": 0})
        assert gmail_sync.index_gmail_mailbox.run(gmail_user.id) == "Reconnect required"

    assert "Reconnect required" in gmail_runs.get_run("index", gmail_user.id)["error"]
    assert _settings(db_session, gmail_user.id)["gmail_reconnect_required"] is True


# --- import_gmail_messages ---------------------------------------------------


def _start(db, user_id, ids, *, sequential=True):
    for position, gmail_id in enumerate(ids, start=1):
        gmail_index_service.upsert_metadata(
            db,
            user_id,
            [
                {
                    "gmail_id": gmail_id,
                    "thread_id": gmail_id,
                    "message_id": f"<{gmail_id}@x>",
                    "sender": "lawyer@example.com",
                    "subject": f"8372/25 Letter {gmail_id}",
                    "sent_at": datetime(2024, 1, position, tzinfo=UTC),
                    "has_attachments": False,
                    "size_estimate": 1,
                }
            ],
        )
    db.commit()
    state = gmail_runs.begin_run(
        "import",
        user_id,
        {
            "run_id": "run-1",
            "total": len(ids),
            "done": 0,
            "remaining": list(ids),
            "failed": [],
            "sequential": sequential,
            "waiting_on": None,
            "waiting_since": None,
            "current": None,
            "cancelled": False,
            "error": None,
        },
    )
    assert state
    return "run-1"


class _Import:
    """Patches around one import hop; records ingests and re-enqueues."""

    def __init__(self, ingest=None, settled=True, locked=True):
        self.ingested: list[bytes] = []
        self.batch_ids = iter(range(100, 200))
        self.ingest = ingest or self._default_ingest
        self.settled = settled
        self.locked = locked

    def _default_ingest(self, _db, raw, owner_id):
        self.ingested.append(raw)
        return SimpleNamespace(id=next(self.batch_ids))

    def hop(self, user_id, run_id="run-1"):
        with (
            patch(
                "app.tasks.gmail_sync.get_gmail_service",
                return_value=GmailConnection(MagicMock(), None),
            ),
            patch(
                "app.tasks.gmail_sync.fetch_raw_message",
                side_effect=lambda _svc, gid: gid.encode(),
            ),
            patch("app.tasks.gmail_sync.ingest_raw_email", side_effect=self.ingest),
            patch("app.tasks.gmail_sync.batch_is_settled", return_value=self.settled),
            patch.object(gmail_sync.import_gmail_messages, "apply_async") as again,
            _locked() as lock,
        ):
            lock.return_value.__enter__.return_value = self.locked
            result = gmail_sync.import_gmail_messages.run(user_id, run_id)
        self.again = again
        return result


def test_sequential_import_ingests_one_message_per_hop_and_waits_for_the_pipeline(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2", "g3"])
    imp = _Import()

    assert imp.hop(gmail_user.id) == "waiting"
    assert imp.ingested == [b"g1"]  # one message, then hand control back
    imp.again.assert_called_once_with(args=[gmail_user.id, "run-1"], countdown=15)
    state = gmail_runs.get_run("import", gmail_user.id)
    assert state["done"] == 1 and state["remaining"] == ["g2", "g3"]
    assert state["waiting_on"] == 100
    assert state["current"]["subject"] == "8372/25 Letter g1"


def test_the_next_message_waits_until_the_last_ones_documents_are_processed(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2"])
    _Import().hop(gmail_user.id)  # g1 ingested, now waiting on its batch

    busy = _Import(settled=False)
    assert busy.hop(gmail_user.id) == "waiting"
    assert busy.ingested == []  # g2 must not start yet
    busy.again.assert_called_once_with(args=[gmail_user.id, "run-1"], countdown=15)

    ready = _Import(settled=True)
    ready.hop(gmail_user.id)
    assert ready.ingested == [b"g2"]


def test_a_batch_that_never_settles_does_not_hold_the_history_forever(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2"])
    _Import().hop(gmail_user.id)
    state = gmail_runs.get_run("import", gmail_user.id)
    state["waiting_since"] = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    gmail_runs.save_run("import", gmail_user.id, state)

    stuck = _Import(settled=False)
    stuck.hop(gmail_user.id)
    assert stuck.ingested == [b"g2"]


def test_a_duplicate_email_has_nothing_to_wait_for(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1", "g2"])
    imp = _Import(ingest=lambda *_a, **_k: None)  # dedup: no new batch
    imp.hop(gmail_user.id)
    assert gmail_runs.get_run("import", gmail_user.id)["waiting_on"] is None
    imp.again.assert_called_once_with(args=[gmail_user.id, "run-1"], countdown=2)


def test_the_last_message_finishes_the_run_and_records_the_result(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1"])
    result = _Import().hop(gmail_user.id)

    assert result == "Imported 1 of 1 messages"
    state = gmail_runs.get_run("import", gmail_user.id)
    assert state["finished_at"] and state["done"] == 1
    sj = _settings(db_session, gmail_user.id)
    assert sj["gmail_last_sync_result"] == "Imported 1 of 1 messages"


def test_a_failing_message_is_tracked_for_retry_and_does_not_stop_the_run(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2"], sequential=False)

    def _ingest(_db, raw, owner_id):
        if raw == b"g1":
            raise ValueError("malformed")
        return SimpleNamespace(id=1)

    result = _Import(ingest=_ingest).hop(gmail_user.id)

    assert result == "Imported 2 of 2 messages"
    state = gmail_runs.get_run("import", gmail_user.id)
    assert state["failed"] == ["g1"]
    sj = _settings(db_session, gmail_user.id)
    assert sj["gmail_failed_message_ids"] == ["g1"]  # the incremental sync retries it
    assert "1 failed" in sj["gmail_last_sync_result"]


def test_non_sequential_import_runs_through_in_one_task_oldest_first(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2", "g3"], sequential=False)
    imp = _Import()
    assert imp.hop(gmail_user.id) == "Imported 3 of 3 messages"
    assert imp.ingested == [b"g1", b"g2", b"g3"]
    imp.again.assert_not_called()


def test_cancelling_stops_after_the_current_message(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1", "g2", "g3"], sequential=False)
    ingested = []

    def _ingest(_db, raw, owner_id):
        ingested.append(raw)
        gmail_runs.cancel_run("import", gmail_user.id)  # user presses Stop mid-run
        return SimpleNamespace(id=1)

    assert _Import(ingest=_ingest).hop(gmail_user.id) == "Import cancelled"
    assert ingested == [b"g1"]
    assert gmail_runs.get_run("import", gmail_user.id)["cancelled"] is True


def test_a_stale_chain_cannot_advance_a_newer_run(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1"])
    imp = _Import()
    assert imp.hop(gmail_user.id, run_id="an-older-run") == "Import no longer active"
    assert imp.ingested == []


def test_a_cancelled_run_does_nothing_on_its_next_hop(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1", "g2"])
    gmail_runs.cancel_run("import", gmail_user.id)
    imp = _Import()
    assert imp.hop(gmail_user.id) == "Import no longer active"
    assert imp.ingested == []


def test_a_busy_mailbox_defers_the_hop_instead_of_dropping_it(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1"])
    imp = _Import(locked=False)
    assert imp.hop(gmail_user.id) == "waiting"
    assert imp.ingested == []
    imp.again.assert_called_once_with(args=[gmail_user.id, "run-1"], countdown=15)


def test_a_revoked_grant_ends_the_run_with_a_reconnect_message(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1"])
    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            side_effect=GmailReconnectRequired("revoked"),
        ),
        _locked() as lock,
    ):
        lock.return_value.__enter__.return_value = True
        assert (
            gmail_sync.import_gmail_messages.run(gmail_user.id, "run-1")
            == "Reconnect required"
        )

    state = gmail_runs.get_run("import", gmail_user.id)
    assert state["finished_at"] and "Reconnect required" in state["error"]
    assert _settings(db_session, gmail_user.id)["gmail_reconnect_required"] is True
