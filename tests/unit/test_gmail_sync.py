"""Tests for the PR1 gmail_sync fixes (2026-09-27 ingestion audit):

- the sync watermark is the run's *start* time (minus overlap), not its end
- one failing message doesn't abort the whole run / block the watermark
- a per-user lock prevents overlapping incremental, index and import runs
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services import user_settings_service
from app.services.ingestion.gmail import GmailConnection
from app.tasks import gmail_sync


class _FakeLockClient:
    """Minimal stand-in for the Redis client used by _user_sync_lock.

    Mirrors just the SET NX EX / GET / DELETE / EVAL(register_script)
    semantics the lock relies on — no real Redis needed, and no fakeredis
    dependency in this repo.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        # Track direct calls separately from what the registered script does
        # internally (via self.store) — a real atomic EVAL never gives the
        # caller a chance to invoke plain GET/DELETE in between.
        self.direct_get_calls = 0
        self.direct_delete_calls = 0

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        self.direct_get_calls += 1
        return self.store.get(key)

    def delete(self, key):
        self.direct_delete_calls += 1
        self.store.pop(key, None)

    def register_script(self, script_body):
        """Only understands _RELEASE_LUA's exact compare-and-delete shape —
        enough to exercise gmail_sync's actual release path, not a general
        Lua interpreter."""
        assert script_body is gmail_sync._RELEASE_LUA

        def _run(keys, args):
            key, token = keys[0], args[0]
            if self.store.get(key) == token:
                del self.store[key]
                return 1
            return 0

        return _run


@pytest.fixture(autouse=True)
def _reset_lock_singletons():
    """gmail_sync caches the lock client and registered release script as
    module globals (same pattern as ocr_slots.py/model_gate.py) — reset
    between tests so a fake client from one test isn't reused by another
    (see tests/unit/test_ocr_slots.py for the established convention)."""
    gmail_sync._lock_client = None
    gmail_sync._release_script = None
    yield
    gmail_sync._lock_client = None
    gmail_sync._release_script = None


@pytest.fixture
def gmail_user(db_session):
    from app.services import auth_service

    user = auth_service.create_user(
        db_session, email="gmailuser@example.com", password="password123"
    )
    user_settings_service.set_gmail_credentials(
        db_session, user.id, credentials_json="{}", connected_at="2026-01-01"
    )
    user_settings_service.set_gmail_inbox_filters(
        db_session, user.id, allowlist=["lawyer@example.com"], label_filter=""
    )
    db_session.commit()
    return user


def _fake_service(messages=None):
    """A MagicMock shaped like the Gmail API client chain the tasks call:
    service.users().messages().list(...).execute() -> {"messages": [...]}."""
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": messages or [],
        "nextPageToken": None,
    }
    return service


@pytest.mark.unit
def test_watermark_is_run_start_not_run_end(gmail_user, db_session):
    """The stored watermark must be `run_started_at - overlap`, anchored to
    when the run *began* — not to when it finished. A slow run (e.g. paginating
    many messages) must not push the watermark forward past messages that
    arrived while it was still running."""
    service = _fake_service(messages=[])

    def _slow_execute():
        import time as _time

        _time.sleep(0.3)
        return {"messages": [], "nextPageToken": None}

    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = _slow_execute

    run_start = datetime.now(UTC)
    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(service, None),
        ),
        patch("app.tasks.gmail_sync._user_sync_lock") as mock_lock,
    ):
        mock_lock.return_value.__enter__.return_value = True
        gmail_sync.sync_gmail_for_user.run(gmail_user.id)
    run_end = datetime.now(UTC)
    assert (run_end - run_start).total_seconds() >= 0.3  # sanity: the sleep ran

    from app.models.database import UserSettings

    settings = (
        db_session.query(UserSettings)
        .filter(UserSettings.user_id == gmail_user.id)
        .first()
    )
    stored = datetime.fromisoformat(settings.settings_json["gmail_last_sync_at"])

    # A start-anchored watermark lands within a couple hundred ms of
    # (run_start - overlap). An end-anchored (buggy) watermark would land
    # ~0.3s later, at (run_end - overlap) — far outside this tolerance.
    expected = run_start - gmail_sync._WATERMARK_OVERLAP
    assert abs((stored - expected).total_seconds()) < 0.2, (
        f"watermark {stored.isoformat()} is not anchored to run start "
        f"({expected.isoformat()}) — looks end-of-run anchored instead"
    )


def _get_settings_json(db_session, user_id):
    from app.models.database import UserSettings

    settings = (
        db_session.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    )
    return settings.settings_json


@pytest.mark.unit
def test_one_failed_message_does_not_abort_the_run_and_is_tracked_for_retry(
    gmail_user, db_session
):
    """A message that fails to fetch/ingest must not crash the whole sync,
    must not block the watermark from advancing for everyone else, and must
    not simply be dropped — it's tracked in gmail_failed_message_ids so a
    future run retries it instead of silently losing that mail forever."""
    service = _fake_service(messages=[{"id": "good-1"}, {"id": "bad-1"}])

    def _fetch(_service, msg_id):
        if msg_id == "bad-1":
            raise ValueError("simulated malformed message")
        return b"raw bytes"

    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(service, None),
        ),
        patch("app.tasks.gmail_sync.fetch_raw_message", side_effect=_fetch),
        patch("app.tasks.gmail_sync.ingest_raw_email") as mock_ingest,
        patch("app.tasks.gmail_sync._user_sync_lock") as mock_lock,
    ):
        mock_lock.return_value.__enter__.return_value = True
        result = gmail_sync.sync_gmail_for_user.run(gmail_user.id)

    assert mock_ingest.call_count == 1  # only the good message reached ingest
    assert "still failing" in result

    sj = _get_settings_json(db_session, gmail_user.id)
    # The watermark must have advanced despite the one failure — it must
    # never gate on a message that might fail forever.
    assert sj.get("gmail_last_sync_at") is not None
    # But the failure itself must be tracked, not silently discarded.
    assert sj.get("gmail_failed_message_ids") == ["bad-1"]


@pytest.mark.unit
def test_previously_failed_message_is_retried_and_drops_off_once_it_succeeds(
    gmail_user, db_session
):
    """A message tracked as failed from a prior run must be retried on the
    next run — and cleared from the tracked list once it succeeds, even if
    it's outside the current watermark window."""
    from app.models.database import UserSettings

    settings = (
        db_session.query(UserSettings)
        .filter(UserSettings.user_id == gmail_user.id)
        .first()
    )
    sj = dict(settings.settings_json or {})
    sj["gmail_failed_message_ids"] = ["retry-me"]
    settings.settings_json = sj
    db_session.commit()

    service = _fake_service(messages=[])  # nothing new this window

    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(service, None),
        ),
        patch("app.tasks.gmail_sync.fetch_raw_message", return_value=b"raw"),
        patch("app.tasks.gmail_sync.ingest_raw_email") as mock_ingest,
        patch("app.tasks.gmail_sync._user_sync_lock") as mock_lock,
    ):
        mock_lock.return_value.__enter__.return_value = True
        result = gmail_sync.sync_gmail_for_user.run(gmail_user.id)

    mock_ingest.assert_called_once()
    assert "still failing" not in result

    sj_after = _get_settings_json(db_session, gmail_user.id)
    assert sj_after.get("gmail_failed_message_ids") == []


@pytest.mark.unit
def test_lock_blocks_overlapping_sync_for_same_user(gmail_user, db_session):
    """A second sync for the same mailbox while one is already in progress
    must be skipped, not run concurrently against the same watermark."""
    fake_client = _FakeLockClient()
    service = _fake_service(messages=[])

    with (
        patch("app.tasks.gmail_sync._get_lock_client", return_value=fake_client),
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(service, None),
        ),
    ):
        # Simulate an in-progress run by holding the lock manually first.
        with gmail_sync._user_sync_lock(gmail_user.id) as first_acquired:
            assert first_acquired is True
            result = gmail_sync.sync_gmail_for_user.run(gmail_user.id)

    assert result == "Sync already in progress"


@pytest.mark.unit
def test_lock_is_released_after_a_successful_run(gmail_user, db_session):
    """The lock must not be left held after the run completes, or every
    subsequent tick would be permanently skipped."""
    fake_client = _FakeLockClient()
    service = _fake_service(messages=[])

    with (
        patch("app.tasks.gmail_sync._get_lock_client", return_value=fake_client),
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(service, None),
        ),
    ):
        gmail_sync.sync_gmail_for_user.run(gmail_user.id)
        # A second call after the first has returned must be able to acquire.
        result = gmail_sync.sync_gmail_for_user.run(gmail_user.id)

    assert result != "Sync already in progress"


@pytest.mark.unit
def test_lock_degrades_open_when_redis_unavailable(gmail_user, db_session):
    """If Redis itself is unreachable, the sync must still run rather than
    permanently refuse to sync — losing the lock is a rare-duplicate-fetch
    risk (mitigated by Message-ID dedup), not a hard gate."""
    import redis

    broken_client = MagicMock()
    broken_client.set.side_effect = redis.ConnectionError("simulated outage")
    service = _fake_service(messages=[])

    with (
        patch("app.tasks.gmail_sync._get_lock_client", return_value=broken_client),
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(service, None),
        ),
    ):
        result = gmail_sync.sync_gmail_for_user.run(gmail_user.id)

    assert result != "Sync already in progress"


@pytest.mark.unit
def test_lock_release_never_deletes_someone_elses_lock(gmail_user):
    """If the TTL lapsed and another run already re-acquired the key under a
    different token, releasing our (stale) handle must not delete their
    lock. (A single-process test can't reproduce the actual timing race a
    GET-then-DELETE has — see the next test for that half of the guarantee —
    but the outcome must still be correct when the token has already
    changed by the time release runs.)"""
    fake_client = _FakeLockClient()

    with patch("app.tasks.gmail_sync._get_lock_client", return_value=fake_client):
        key = f"{gmail_sync._LOCK_PREFIX}{gmail_user.id}"
        with gmail_sync._user_sync_lock(gmail_user.id):
            # Simulate our TTL expiring and a different run re-acquiring the
            # same key under a new token while we still think we hold it.
            fake_client.store[key] = "someone-elses-token"
        # Our __exit__ just ran and tried to release with our own (stale)
        # token — the other run's lock must have survived.
        assert fake_client.store.get(key) == "someone-elses-token"


@pytest.mark.unit
def test_lock_release_uses_a_single_atomic_call_not_separate_get_and_delete(
    gmail_user,
):
    """The actual atomicity guarantee: release must never issue GET and
    DELETE as two separate round trips (that gap is the TOCTOU window a
    single-process test can't otherwise reproduce timing for). It must
    compare-and-delete inside one opaque call (register_script(...)(...))
    instead, so there is no gap for another process to land in."""
    fake_client = _FakeLockClient()

    with patch("app.tasks.gmail_sync._get_lock_client", return_value=fake_client):
        with gmail_sync._user_sync_lock(gmail_user.id):
            pass

    # This is what a GET-then-DELETE implementation would necessarily do —
    # and what the pre-fix code did. The atomic script path does its
    # compare-and-delete against fake_client.store directly, inside the
    # script closure, never through these tracked public methods.
    assert fake_client.direct_get_calls == 0
    assert fake_client.direct_delete_calls == 0


@pytest.mark.unit
def test_index_refresh_retries_on_lock_collision_instead_of_silently_no_opping(
    gmail_user, db_session
):
    """A user-triggered index refresh must not silently do nothing when it loses
    the lock race — it's an explicit user action, not a background tick, so
    it should defer and retry rather than leave the user thinking it ran."""
    from celery.exceptions import Retry

    fake_client = _FakeLockClient()
    retry_sentinel = Retry()

    with (
        patch("app.tasks.gmail_sync._get_lock_client", return_value=fake_client),
        patch.object(
            gmail_sync.index_gmail_mailbox, "retry", side_effect=retry_sentinel
        ) as mock_retry,
    ):
        with gmail_sync._user_sync_lock(gmail_user.id):
            with pytest.raises(Retry):
                gmail_sync.index_gmail_mailbox.run(gmail_user.id)

    mock_retry.assert_called_once_with(countdown=30)


# --- Watermark safety, outcome recording, token persistence ------------------


def list_call(service):
    return service.users.return_value.messages.return_value.list


def _set_settings(db_session, user_id, **changes):
    from app.models.database import UserSettings

    settings = db_session.query(UserSettings).filter_by(user_id=user_id).one()
    data = dict(settings.settings_json or {})
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    settings.settings_json = data
    db_session.commit()


def _run_sync(user_id, service, **patches):
    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(service, patches.pop("refreshed", None)),
        ),
        patch("app.tasks.gmail_sync.fetch_raw_message", return_value=b"raw"),
        patch("app.tasks.gmail_sync.ingest_raw_email"),
        patch("app.tasks.gmail_sync._user_sync_lock") as mock_lock,
    ):
        mock_lock.return_value.__enter__.return_value = True
        return gmail_sync.sync_gmail_for_user.run(user_id)


@pytest.mark.unit
def test_missing_watermark_is_anchored_and_never_queries_unbounded(
    gmail_user, db_session
):
    _set_settings(db_session, gmail_user.id, gmail_last_sync_at=None)
    service = _fake_service(messages=[{"id": "m1"}])

    result = _run_sync(gmail_user.id, service)

    assert result == "Initialized sync watermark"
    service.users.return_value.messages.return_value.list.assert_not_called()
    db_session.expire_all()
    assert _get_settings_json(db_session, gmail_user.id)["gmail_last_sync_at"]


@pytest.mark.unit
def test_incremental_query_respects_the_label_filter(gmail_user, db_session):
    user_settings_service.set_gmail_inbox_filters(
        db_session,
        gmail_user.id,
        allowlist=["lawyer@example.com"],
        label_filter="Sanctuary",
    )
    db_session.commit()
    service = _fake_service()
    _run_sync(gmail_user.id, service)
    q = list_call(service).call_args.kwargs["q"]
    assert "label:Sanctuary" in q and "after:" in q


@pytest.mark.unit
def test_success_records_result_and_clears_a_previous_error(gmail_user, db_session):
    _set_settings(
        db_session,
        gmail_user.id,
        gmail_last_sync_error="old failure",
        gmail_reconnect_required=True,
    )

    result = _run_sync(gmail_user.id, _fake_service())

    db_session.expire_all()
    sj = _get_settings_json(db_session, gmail_user.id)
    assert sj["gmail_last_sync_result"] == result
    assert sj["gmail_last_sync_error"] is None
    assert sj["gmail_reconnect_required"] is False


@pytest.mark.unit
def test_reconnect_required_is_recorded_and_not_retried(gmail_user, db_session):
    from app.services.ingestion.gmail import GmailReconnectRequired

    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            side_effect=GmailReconnectRequired("token revoked"),
        ),
        patch("app.tasks.gmail_sync._user_sync_lock") as mock_lock,
    ):
        mock_lock.return_value.__enter__.return_value = True
        # Returns (does not raise) so Celery's autoretry doesn't hammer Google.
        result = gmail_sync.sync_gmail_for_user.run(gmail_user.id)

    assert result == "Reconnect required"
    db_session.expire_all()
    sj = _get_settings_json(db_session, gmail_user.id)
    assert sj["gmail_last_sync_error"] == "token revoked"
    assert sj["gmail_reconnect_required"] is True


@pytest.mark.unit
def test_undecryptable_credentials_require_reconnect(gmail_user, db_session):
    from app.core.secrets import SecretsError

    with (
        patch(
            "app.tasks.gmail_sync.user_settings_service.decrypt_gmail_credentials",
            side_effect=SecretsError("wrong key"),
        ),
        patch("app.tasks.gmail_sync._user_sync_lock") as mock_lock,
    ):
        mock_lock.return_value.__enter__.return_value = True
        assert gmail_sync.sync_gmail_for_user.run(gmail_user.id) == "Reconnect required"
    db_session.expire_all()
    assert _get_settings_json(db_session, gmail_user.id)["gmail_reconnect_required"]


@pytest.mark.unit
def test_unexpected_failure_is_recorded_and_still_retried(gmail_user, db_session):
    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service", side_effect=RuntimeError("boom")
        ),
        patch("app.tasks.gmail_sync._user_sync_lock") as mock_lock,
    ):
        mock_lock.return_value.__enter__.return_value = True
        with pytest.raises(RuntimeError):
            gmail_sync.sync_gmail_for_user.run(gmail_user.id)
    db_session.expire_all()
    sj = _get_settings_json(db_session, gmail_user.id)
    assert sj["gmail_last_sync_error"] == "boom"
    assert sj["gmail_reconnect_required"] is False


@pytest.mark.unit
def test_refreshed_token_is_persisted_encrypted_and_unchanged_one_is_not(
    gmail_user, db_session
):
    before = _get_settings_json(db_session, gmail_user.id)["gmail_credentials_json"]

    _run_sync(gmail_user.id, _fake_service())  # token unchanged
    db_session.expire_all()
    assert (
        _get_settings_json(db_session, gmail_user.id)["gmail_credentials_json"]
        == before
    )

    _run_sync(gmail_user.id, _fake_service(), refreshed='{"token": "fresh"}')
    db_session.expire_all()
    after = _get_settings_json(db_session, gmail_user.id)["gmail_credentials_json"]
    assert after != before and after.startswith("enc:v1:")
    assert (
        user_settings_service.decrypt_gmail_credentials(after) == '{"token": "fresh"}'
    )


@pytest.mark.unit
def test_disconnect_during_a_sync_is_not_resurrected(gmail_user, db_session):
    """If the user disconnects while a run is fetching, the run's final write
    must not put the watermark/result of a connection that no longer exists
    back into settings."""
    from app.models.database import UserSettings

    def _disconnect_mid_run(*_args, **_kwargs):
        user_settings_service.clear_gmail_connection(db_session, gmail_user.id)
        db_session.commit()
        return 0, []

    with (
        patch(
            "app.tasks.gmail_sync.get_gmail_service",
            return_value=GmailConnection(_fake_service(), None),
        ),
        patch("app.tasks.gmail_sync._ingest_query", side_effect=_disconnect_mid_run),
        patch("app.tasks.gmail_sync._user_sync_lock") as mock_lock,
    ):
        mock_lock.return_value.__enter__.return_value = True
        result = gmail_sync.sync_gmail_for_user.run(gmail_user.id)

    assert result == "Gmail disconnected during sync"
    db_session.expire_all()
    sj = (
        db_session.query(UserSettings)
        .filter_by(user_id=gmail_user.id)
        .one()
        .settings_json
    )
    for key in (
        "gmail_credentials_json",
        "gmail_last_sync_at",
        "gmail_last_sync_result",
    ):
        assert key not in sj
