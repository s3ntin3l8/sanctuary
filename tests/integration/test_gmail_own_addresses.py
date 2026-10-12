"""The user's own addresses are read from the connected Gmail account."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from app.models.database import UserSettings
from app.services.ingestion import gmail
from app.services.ingestion.gmail import GmailConnection, fetch_own_addresses

FRESH = datetime.now(UTC).isoformat()


def _service(profile="Me@Example.com", aliases=None, alias_error=None):
    service = MagicMock()
    users = service.users.return_value
    users.getProfile.return_value.execute.return_value = {"emailAddress": profile}
    send_as = users.settings.return_value.sendAs.return_value.list.return_value
    if alias_error:
        send_as.execute.side_effect = alias_error
    else:
        send_as.execute.return_value = {"sendAs": aliases or []}
    return service


def test_primary_address_plus_verified_aliases_lowercased_and_deduped():
    service = _service(
        aliases=[
            {"sendAsEmail": "me@example.com", "verificationStatus": "accepted"},
            {"sendAsEmail": "Work@Firm.de"},  # the primary carries no status
            {"sendAsEmail": "pending@example.com", "verificationStatus": "pending"},
        ]
    )
    assert fetch_own_addresses(service) == ["me@example.com", "work@firm.de"]


def _setup(db, **settings):
    from tests.integration.test_gmail_controls import _admin, _connect

    uid = _admin(db)
    _connect(db, uid, **settings)
    row = db.query(UserSettings).filter_by(user_id=uid).one()
    return uid, dict(row.settings_json)


def _connect_with(db, uid, sj, service):
    with patch.object(
        gmail, "get_gmail_service", return_value=GmailConnection(service, None)
    ):
        gmail.connect_gmail(db, uid, sj)
    db.expire_all()
    return db.query(UserSettings).filter_by(user_id=uid).one().settings_json


def test_connect_fills_in_missing_addresses(db_session):
    uid, sj = _setup(db_session)
    stored = _connect_with(db_session, uid, sj, _service())
    assert stored["gmail_own_addresses"] == ["me@example.com"]
    assert stored["gmail_own_addresses_checked_at"]


def test_connect_leaves_a_fresh_list_alone(db_session):
    uid, sj = _setup(
        db_session,
        gmail_own_addresses=["kept@example.com"],
        gmail_own_addresses_checked_at=FRESH,
    )
    service = _service()
    stored = _connect_with(db_session, uid, sj, service)
    assert stored["gmail_own_addresses"] == ["kept@example.com"]
    service.users.return_value.getProfile.assert_not_called()


def test_connect_refreshes_a_day_old_list(db_session):
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    uid, sj = _setup(
        db_session,
        gmail_own_addresses=["old@example.com"],
        gmail_own_addresses_checked_at=old,
    )
    stored = _connect_with(db_session, uid, sj, _service())
    assert stored["gmail_own_addresses"] == ["me@example.com"]


def test_lookup_failure_keeps_the_old_list_and_does_not_raise(db_session):
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    uid, sj = _setup(
        db_session,
        gmail_own_addresses=["old@example.com"],
        gmail_own_addresses_checked_at=old,
    )
    stored = _connect_with(db_session, uid, sj, _service(alias_error=OSError("down")))
    assert stored["gmail_own_addresses"] == ["old@example.com"]
    assert stored["gmail_own_addresses_checked_at"] == old
