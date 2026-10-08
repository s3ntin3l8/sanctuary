"""index_gmail_mailbox and import_gmail_messages (the history import tasks)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.database import GmailMessageIndex, UserSettings
from app.services import (
    gmail_cache,
    gmail_index_service,
    gmail_runs,
    user_settings_service,
)
from app.services.ingestion.gmail import GmailConnection, GmailReconnectRequired
from app.tasks import gmail_sync

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _runs(fake_run_state):
    return fake_run_state


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
    return patch("app.tasks.gmail_sync._user_sync_lock")


def _settings(db, user_id):
    db.expire_all()
    return db.query(UserSettings).filter_by(user_id=user_id).one().settings_json


# --- index_gmail_mailbox -----------------------------------------------------


def _run_index(user_id, ids, metadata, *, run_id="idx-1", begin=True):
    with (
        patch(
            "app.services.ingestion.gmail.get_gmail_service",
            return_value=GmailConnection(MagicMock(), None),
        ),
        patch(
            "app.tasks.gmail_sync.list_message_ids", return_value=iter(ids)
        ) as listed,
        patch("app.tasks.gmail_sync.fetch_metadata", side_effect=metadata) as fetched,
        _locked() as lock,
    ):
        lock.return_value.__enter__.return_value = True
        if begin:
            gmail_runs.begin_run(
                "index",
                user_id,
                {"run_id": run_id, "total": 0, "done": 0, "skipped": 0},
            )
        result = gmail_sync.index_gmail_mailbox.run(user_id, run_id)
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
    assert "from:lawyer@example.com" in query and 'label:"Sanctuary"' in query
    assert "after:" not in query
    rows = {r.gmail_id: r for r in db_session.query(GmailMessageIndex)}
    assert rows["g1"].internal_id == "8372-25"
    assert (
        rows["g2"].internal_id is None and rows["g2"].group_key == "8372-25"
    )  # thread
    state = gmail_runs.get_run("index", gmail_user.id)
    assert state["finished_at"] and (state["done"], state["total"]) == (2, 2)
    assert state["skipped"] == 0


def test_index_refresh_only_fetches_ids_it_does_not_have(gmail_user, db_session):
    _run_index(
        gmail_user.id,
        ["g1", "g2"],
        [[_raw("g1", "8372/25 a", 1), _raw("g2", "8372/25 b", 2)]],
        run_id="idx-1",
    )
    _, _, fetched = _run_index(
        gmail_user.id,
        ["g3", "g1", "g2"],
        [[_raw("g3", "8372/25 c", 3)]],
        run_id="idx-2",
    )

    assert fetched.call_args.args[1] == ["g3"]
    assert db_session.query(GmailMessageIndex).count() == 3


def test_messages_gmail_did_not_return_are_counted_not_hidden(gmail_user, db_session):
    broken = {"id": "bad", "payload": {"headers": []}}  # no Date and no internalDate
    # g2 is requested but missing from Gmail's batch reply (quota/5xx).
    result, _, _ = _run_index(
        gmail_user.id, ["g1", "bad", "g2"], [[_raw("g1", "x", 1), broken]]
    )
    assert db_session.query(GmailMessageIndex).count() == 1
    state = gmail_runs.get_run("index", gmail_user.id)
    assert state["skipped"] == 2  # one unparseable, one never returned
    assert "Indexed 1" in result


def test_one_malformed_message_does_not_lose_the_rest_of_the_chunk(
    gmail_user, db_session
):
    # No Date header and a null internalDate: parse_metadata raises TypeError.
    null_date = {"id": "bad", "internalDate": None, "payload": {"headers": []}}
    no_payload_headers = {"id": "worse", "internalDate": "0", "payload": None}
    result, _, _ = _run_index(
        gmail_user.id,
        ["g1", "bad", "worse", "g2"],
        [
            [
                _raw("g1", "8372/25 a", 1),
                null_date,
                no_payload_headers,
                _raw("g2", "8372/25 b", 2),
            ]
        ],
    )
    assert {r.gmail_id for r in db_session.query(GmailMessageIndex)} >= {"g1", "g2"}
    assert gmail_runs.get_run("index", gmail_user.id)["finished_at"]
    assert "Indexed" in result


def test_index_without_any_filter_explains_itself(gmail_user, db_session):
    user_settings_service.set_gmail_inbox_filters(
        db_session, gmail_user.id, allowlist=[], label_filter=""
    )
    db_session.commit()
    result, listed, _ = _run_index(gmail_user.id, [], [])
    assert result == "Not configured"
    listed.assert_not_called()
    assert "allowlist or a label" in gmail_runs.get_run("index", gmail_user.id)["error"]


def test_index_needing_a_reconnect_is_recorded_not_retried(gmail_user, db_session):
    with (
        patch(
            "app.services.ingestion.gmail.get_gmail_service",
            side_effect=GmailReconnectRequired("revoked"),
        ),
        _locked() as lock,
    ):
        lock.return_value.__enter__.return_value = True
        gmail_runs.begin_run("index", gmail_user.id, {"run_id": "idx-1"})
        assert (
            gmail_sync.index_gmail_mailbox.run(gmail_user.id, "idx-1")
            == "Reconnect required"
        )

    assert "Reconnect required" in gmail_runs.get_run("index", gmail_user.id)["error"]
    assert _settings(db_session, gmail_user.id)["gmail_reconnect_required"] is True


def _failing_index(user_id, *, retries_left):
    gmail_runs.begin_run("index", user_id, {"run_id": "idx-1"})
    with (
        patch(
            "app.services.ingestion.gmail.get_gmail_service",
            side_effect=RuntimeError("boom"),
        ),
        patch.object(
            gmail_sync.index_gmail_mailbox, "max_retries", 0 if not retries_left else 3
        ),
        _locked() as lock,
    ):
        lock.return_value.__enter__.return_value = True
        with pytest.raises(RuntimeError):
            gmail_sync.index_gmail_mailbox.run(user_id, "idx-1")


def test_an_index_failure_that_celery_will_retry_keeps_the_run_active(gmail_user):
    _failing_index(gmail_user.id, retries_left=True)
    state = gmail_runs.get_run("index", gmail_user.id)
    # Still running (Refresh stays disabled; a second run can't start) but shows why.
    assert gmail_runs.is_active(state) and state["error"] == "boom"


def test_the_last_failed_attempt_ends_the_run(gmail_user):
    _failing_index(gmail_user.id, retries_left=False)
    state = gmail_runs.get_run("index", gmail_user.id)
    assert not gmail_runs.is_active(state) and state["error"] == "boom"


def test_a_retry_clears_the_previous_attempts_error(gmail_user, db_session):
    _failing_index(gmail_user.id, retries_left=True)
    result, _, _ = _run_index(
        gmail_user.id,
        ["g1"],
        [[_raw("g1", "8372/25 a", 1)]],
        run_id="idx-1",
        begin=False,
    )
    state = gmail_runs.get_run("index", gmail_user.id)
    assert "Indexed 1" in result and state["error"] is None and state["finished_at"]


def test_a_superseded_index_run_stops(gmail_user, db_session):
    _run_index(
        gmail_user.id, ["g1"], [[_raw("g1", "x", 1)]], run_id="idx-old", begin=False
    )
    assert db_session.query(GmailMessageIndex).count() == 0  # nothing owned by that run


# --- import_gmail_messages ---------------------------------------------------


def _start(db, user_id, ids):
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
            "current": None,
            "cancelled": False,
            "error": None,
        },
    )
    assert state
    return "run-1"


class _Import:
    """Patches around one import task run; records ingests and re-enqueues."""

    def __init__(self, ingest=None, locked=True):
        self.fetched: list[str] = []  # ids actually requested from Gmail
        self.connect = MagicMock(return_value=GmailConnection(MagicMock(), None))
        self.ingested: list[bytes] = []
        self.batch_ids = iter(range(100, 200))
        self.ingest = ingest or self._default_ingest
        self.locked = locked

    def _default_ingest(self, _db, raw, owner_id):
        self.ingested.append(raw)
        return SimpleNamespace(id=next(self.batch_ids))

    def _fetch(self, _service, gmail_id):
        self.fetched.append(gmail_id)
        return gmail_id.encode()

    def run(self, user_id, run_id="run-1"):
        with (
            patch("app.services.ingestion.gmail.get_gmail_service", self.connect),
            patch("app.tasks.gmail_sync.fetch_raw_message", side_effect=self._fetch),
            patch("app.tasks.gmail_sync.ingest_raw_email", side_effect=self.ingest),
            patch.object(gmail_sync.import_gmail_messages, "apply_async") as again,
            _locked() as lock,
        ):
            lock.return_value.__enter__.return_value = self.locked
            result = gmail_sync.import_gmail_messages.run(user_id, run_id)
        self.again = again
        return result


def _state(user_id):
    return gmail_runs.get_run("import", user_id)


def test_import_does_not_wait_for_the_pipeline_between_emails(gmail_user, db_session):
    """Every email is ingested right away; nothing paces on document processing, so
    extraction and the per-document AI stages overlap across the whole selection."""
    _start(db_session, gmail_user.id, ["g1", "g2", "g3"])
    imp = _Import()

    assert imp.run(gmail_user.id) == "Imported 3 of 3 messages"
    assert imp.ingested == [b"g1", b"g2", b"g3"]  # chronological, so ids are too
    imp.again.assert_not_called()  # no hand-offs, no countdowns
    state = _state(gmail_user.id)
    assert state["done"] == 3 and state["finished_at"]


def test_a_redelivered_task_resumes_from_stored_progress(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1", "g2", "g3"])
    gmail_runs.update_run(
        "import", gmail_user.id, "run-1", {"done": 1, "remaining": ["g2", "g3"]}
    )

    imp = _Import()
    assert imp.run(gmail_user.id) == "Imported 3 of 3 messages"
    assert imp.ingested == [b"g2", b"g3"]  # g1 was already done: not ingested again


def test_a_task_that_arrives_after_the_run_finished_does_nothing(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1"])
    _Import().run(gmail_user.id)

    late = _Import()  # the broker delivers the task a second time
    assert late.run(gmail_user.id) == "Import no longer active"
    assert late.ingested == []


def test_a_busy_mailbox_defers_the_import_instead_of_dropping_it(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1"])
    before = _state(gmail_user.id)["updated_at"]
    imp = _Import(locked=False)

    assert imp.run(gmail_user.id) == "waiting"
    assert imp.ingested == []
    imp.again.assert_called_once_with(args=[gmail_user.id, "run-1"], countdown=15)
    assert _state(gmail_user.id)["updated_at"] > before  # heartbeat: not 'abandoned'


def test_the_last_message_finishes_the_run_and_records_the_result(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1"])
    result = _Import().run(gmail_user.id)

    assert result == "Imported 1 of 1 messages"
    state = _state(gmail_user.id)
    assert state["finished_at"] and state["done"] == 1
    sj = _settings(db_session, gmail_user.id)
    assert sj["gmail_last_sync_result"] == "Imported 1 of 1 messages"


def test_a_failing_message_is_tracked_for_retry_and_does_not_stop_the_run(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2"])

    def _ingest(_db, raw, owner_id):
        if raw == b"g1":
            raise ValueError("malformed")
        return SimpleNamespace(id=1)

    result = _Import(ingest=_ingest).run(gmail_user.id)

    assert result == "Imported 2 of 2 messages"
    assert _state(gmail_user.id)["failed"] == ["g1"]
    sj = _settings(db_session, gmail_user.id)
    assert sj["gmail_failed_message_ids"] == ["g1"]  # the incremental sync retries it
    assert "1 failed" in sj["gmail_last_sync_result"]


def test_the_whole_selection_is_ingested_in_one_task_oldest_first(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2", "g3"])
    imp = _Import()
    assert imp.run(gmail_user.id) == "Imported 3 of 3 messages"
    assert imp.ingested == [b"g1", b"g2", b"g3"]
    imp.again.assert_not_called()


def test_cancelling_stops_after_the_current_message_and_keeps_the_cancel(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2", "g3"])
    ingested = []

    def _ingest(_db, raw, owner_id):
        ingested.append(raw)
        gmail_runs.cancel_run("import", gmail_user.id)  # user presses Stop mid-run
        return SimpleNamespace(id=1)

    assert _Import(ingest=_ingest).run(gmail_user.id) == "Import cancelled"
    assert ingested == [b"g1"]
    state = _state(gmail_user.id)
    assert state["cancelled"] is True and state["remaining"] == []  # not overwritten
    assert _settings(db_session, gmail_user.id)["gmail_last_sync_result"] == (
        "Cancelled after 1 of 3 messages"
    )


def test_failures_before_a_cancel_still_reach_the_retry_list(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1", "g2"])

    def _ingest(_db, raw, owner_id):
        gmail_runs.cancel_run("import", gmail_user.id)
        raise ValueError("malformed")

    _Import(ingest=_ingest).run(gmail_user.id)

    sj = _settings(db_session, gmail_user.id)
    assert sj["gmail_failed_message_ids"] == ["g1"]
    assert "Cancelled after 1 of 2" in sj["gmail_last_sync_result"]


def test_an_old_task_finishing_cannot_clobber_a_newer_run(gmail_user, db_session):
    """Stop, then immediately start a new import, while the old task is still
    ingesting its last message."""
    _start(db_session, gmail_user.id, ["g1"])

    def _ingest(_db, raw, owner_id):
        gmail_runs.cancel_run("import", gmail_user.id)
        new = gmail_runs.begin_run(
            "import",
            gmail_user.id,
            {
                "run_id": "run-2",
                "total": 5,
                "done": 0,
                "remaining": list("abcde"),
                "failed": [],
            },
        )
        assert new
        return SimpleNamespace(id=1)

    _Import(ingest=_ingest).run(gmail_user.id)

    state = _state(gmail_user.id)
    assert state["run_id"] == "run-2" and gmail_runs.is_active(state)
    assert state["remaining"] == list("abcde") and state["done"] == 0


def test_a_task_of_an_older_run_cannot_advance_a_newer_one(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1"])
    imp = _Import()
    assert imp.run(gmail_user.id, run_id="an-older-run") == "Import no longer active"
    assert imp.ingested == []


def test_a_cancelled_run_does_nothing_when_its_task_starts(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1", "g2"])
    gmail_runs.cancel_run("import", gmail_user.id)
    imp = _Import()
    assert imp.run(gmail_user.id) == "Import no longer active"
    assert imp.ingested == []


def test_a_revoked_grant_ends_the_run_with_a_reconnect_message(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1"])
    with (
        patch(
            "app.services.ingestion.gmail.get_gmail_service",
            side_effect=GmailReconnectRequired("revoked"),
        ),
        _locked() as lock,
    ):
        lock.return_value.__enter__.return_value = True
        assert (
            gmail_sync.import_gmail_messages.run(gmail_user.id, "run-1")
            == "Reconnect required"
        )

    state = _state(gmail_user.id)
    assert state["finished_at"] and "Reconnect required" in state["error"]
    assert _settings(db_session, gmail_user.id)["gmail_reconnect_required"] is True


# --- local cache -------------------------------------------------------------


def test_fetched_mail_is_cached_before_it_is_ingested(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1"])

    def _ingest(_db, raw, owner_id):
        # By the time ingest runs, the raw message is already safely on disk.
        assert gmail_cache.read(gmail_user.id, "g1") == b"g1"
        return SimpleNamespace(id=1)

    imp = _Import(ingest=_ingest)
    imp.run(gmail_user.id)
    assert imp.fetched == ["g1"]


def test_a_message_that_fails_to_ingest_is_still_cached(gmail_user, db_session):
    _start(db_session, gmail_user.id, ["g1"])

    def _boom(*_a, **_k):
        raise ValueError("parser bug")

    _Import(ingest=_boom).run(gmail_user.id)
    assert gmail_cache.read(gmail_user.id, "g1") == b"g1"  # can be replayed once fixed


def test_cached_mail_is_imported_without_touching_gmail(gmail_user, db_session):
    for gid in ("g1", "g2"):
        gmail_cache.write(gmail_user.id, gid, f"cached {gid}".encode())
    _start(db_session, gmail_user.id, ["g1", "g2"])

    imp = _Import()
    assert imp.run(gmail_user.id) == "Imported 2 of 2 messages"

    assert imp.ingested == [b"cached g1", b"cached g2"]
    assert imp.fetched == []  # no Gmail fetch...
    imp.connect.assert_not_called()  # ...and no Gmail connection at all


def test_only_cache_misses_cost_a_gmail_connection(gmail_user, db_session):
    gmail_cache.write(gmail_user.id, "g1", b"cached g1")
    _start(db_session, gmail_user.id, ["g1", "g2", "g3"])

    imp = _Import()
    imp.run(gmail_user.id)

    assert imp.ingested == [b"cached g1", b"g2", b"g3"]
    assert imp.fetched == ["g2", "g3"]
    assert imp.connect.call_count == 1  # built once, on the first miss


def test_cached_mail_imports_even_when_the_grant_is_revoked(gmail_user, db_session):
    gmail_cache.write(gmail_user.id, "g1", b"cached g1")
    _start(db_session, gmail_user.id, ["g1"])
    imp = _Import()
    imp.connect.side_effect = GmailReconnectRequired("revoked")

    assert imp.run(gmail_user.id) == "Imported 1 of 1 messages"
    assert imp.ingested == [b"cached g1"]


def test_a_revoked_grant_on_a_cache_miss_ends_the_run_instead_of_failing_the_message(
    gmail_user, db_session
):
    _start(db_session, gmail_user.id, ["g1", "g2"])
    imp = _Import()
    imp.connect.side_effect = GmailReconnectRequired("revoked")

    assert imp.run(gmail_user.id) == "Reconnect required"
    state = _state(gmail_user.id)
    assert state["failed"] == []  # the message isn't blamed for the dead token
    assert "Reconnect required" in state["error"]


def test_a_wipe_followed_by_a_reimport_is_deterministic_and_offline(
    gmail_user, db_session
):
    from app.services.maintenance_service import clear_all_data

    _start(db_session, gmail_user.id, ["g1", "g2"])
    first = _Import()
    first.run(gmail_user.id)
    assert first.fetched == ["g1", "g2"]

    clear_all_data(db_session)  # Settings -> Data -> Clear all data

    assert gmail_cache.stats(gmail_user.id)[0] == 2  # the cache outlived the wipe
    _start(db_session, gmail_user.id, ["g1", "g2"])
    again = _Import()
    again.run(gmail_user.id)
    assert again.fetched == [] and again.ingested == [b"g1", b"g2"]
    again.connect.assert_not_called()
