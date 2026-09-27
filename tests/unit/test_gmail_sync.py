"""Tests for the PR1 gmail_sync fixes (2026-09-27 ingestion audit):

- the sync watermark is the run's *start* time (minus overlap), not its end
- one failing message doesn't abort the whole run / block the watermark
- a per-user lock prevents overlapping incremental + backfill runs
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services import user_settings_service
from app.tasks import gmail_sync


class _FakeLockClient:
    """Minimal stand-in for the Redis client used by _user_sync_lock.

    Mirrors just the SET NX EX / GET / DELETE / EVAL(register_script)
    semantics the lock relies on — no real Redis needed, and no fakeredis
    dependency in this repo.
    """

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
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
        patch("app.tasks.gmail_sync.get_gmail_service", return_value=service),
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
        patch("app.tasks.gmail_sync.get_gmail_service", return_value=service),
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
        patch("app.tasks.gmail_sync.get_gmail_service", return_value=service),
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
        patch("app.tasks.gmail_sync.get_gmail_service", return_value=service),
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
        patch("app.tasks.gmail_sync.get_gmail_service", return_value=service),
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
        patch("app.tasks.gmail_sync.get_gmail_service", return_value=service),
    ):
        result = gmail_sync.sync_gmail_for_user.run(gmail_user.id)

    assert result != "Sync already in progress"


@pytest.mark.unit
def test_lock_release_is_atomic_and_never_deletes_someone_elses_lock(gmail_user):
    """Release must be a compare-and-delete, not GET-then-DELETE: if the TTL
    lapsed and another run already re-acquired the key under a different
    token, releasing our (stale) handle must not delete their lock."""
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
def test_backfill_retries_on_lock_collision_instead_of_silently_no_opping(
    gmail_user, db_session
):
    """A user-triggered backfill must not silently do nothing when it loses
    the lock race — it's an explicit user action, not a background tick, so
    it should defer and retry rather than leave the user thinking it ran."""
    from celery.exceptions import Retry

    fake_client = _FakeLockClient()
    retry_sentinel = Retry()

    with (
        patch("app.tasks.gmail_sync._get_lock_client", return_value=fake_client),
        patch.object(
            gmail_sync.run_gmail_backfill, "retry", side_effect=retry_sentinel
        ) as mock_retry,
    ):
        with gmail_sync._user_sync_lock(gmail_user.id):
            with pytest.raises(Retry):
                gmail_sync.run_gmail_backfill.run(gmail_user.id)

    mock_retry.assert_called_once_with(countdown=30)
