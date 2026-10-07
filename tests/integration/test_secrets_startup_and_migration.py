"""Fail-closed startup check and the credential-encryption data migration."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from app import config
from app.core import secrets
from app.models.database import AppSettings, User, UserSettings

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/f6b2d8a4c1e7_encrypt_stored_credentials.py"
)
CREDS = '{"refresh_token": "r"}'  # pragma: allowlist secret
LIVE_KEY = "sk-live-1"  # pragma: allowlist secret
NO_KEY = "not-needed"  # pragma: allowlist secret


def _load_migration():
    spec = importlib.util.spec_from_file_location("encrypt_creds_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _admin_settings(db) -> UserSettings:
    uid = db.query(User).filter_by(email="admin@localhost").one().id
    row = db.query(UserSettings).filter_by(user_id=uid).first()
    if row is None:
        row = UserSettings(user_id=uid, settings_json={})
        db.add(row)
        db.flush()
    return row


def _app_settings(db) -> AppSettings:
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(settings_json={})
        db.add(row)
        db.flush()
    return row


def _seed_plaintext(db):
    _admin_settings(db).settings_json = {"gmail_credentials_json": CREDS, "keep": 1}
    _app_settings(db).settings_json = {
        "ai": {
            "instances": [
                {"id": "a", "api_key": LIVE_KEY},
                {"id": "b", "api_key": NO_KEY},
                {"id": "c", "api_key": ""},
            ]
        }
    }
    db.commit()


def _read(db):
    db.expire_all()
    creds = _admin_settings(db).settings_json["gmail_credentials_json"]
    instances = {
        i["id"]: i["api_key"]
        for i in _app_settings(db).settings_json["ai"]["instances"]
    }
    return creds, instances


def test_migration_encrypts_plaintext_and_downgrade_restores_it(db_session):
    _seed_plaintext(db_session)
    migration = _load_migration()

    with patch("alembic.op.get_bind", return_value=db_session.connection()):
        migration.upgrade()
    creds, instances = _read(db_session)
    assert creds.startswith(secrets.PREFIX) and "refresh_token" not in creds
    assert instances["a"].startswith(secrets.PREFIX)
    assert instances["b"] == NO_KEY and instances["c"] == ""  # not secrets
    assert _admin_settings(db_session).settings_json["keep"] == 1

    with patch("alembic.op.get_bind", return_value=db_session.connection()):
        migration.upgrade()  # idempotent: already-encrypted values are left alone
    assert _read(db_session) == (creds, instances)

    with patch("alembic.op.get_bind", return_value=db_session.connection()):
        migration.downgrade()
    creds, instances = _read(db_session)
    assert creds == CREDS
    assert instances["a"] == LIVE_KEY


def test_migration_without_secrets_needs_no_key(db_session, monkeypatch):
    _admin_settings(db_session).settings_json = {"theme": "dark"}
    _app_settings(db_session).settings_json = {"ai": {"instances": []}}
    db_session.commit()
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with patch("alembic.op.get_bind", return_value=db_session.connection()):
        _load_migration().upgrade()  # must not raise


def test_migration_with_plaintext_but_no_key_fails_with_instructions(
    db_session, monkeypatch
):
    _seed_plaintext(db_session)
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with (
        patch("alembic.op.get_bind", return_value=db_session.connection()),
        pytest.raises(secrets.SecretsError, match="SECRETS_ENCRYPTION_KEY"),
    ):
        _load_migration().upgrade()


def test_startup_fails_closed_when_encrypted_secrets_exist_without_a_key(
    db_session, monkeypatch
):
    _admin_settings(db_session).settings_json = {
        "gmail_credentials_json": secrets.encrypt(CREDS)
    }
    db_session.commit()

    secrets.require_key_if_secrets_stored(db_session)  # key present: fine
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with pytest.raises(secrets.SecretsError, match="SECRETS_ENCRYPTION_KEY"):
        secrets.require_key_if_secrets_stored(db_session)


def test_startup_without_key_is_fine_when_nothing_is_encrypted(db_session, monkeypatch):
    _admin_settings(db_session).settings_json = {"theme": "dark"}
    _app_settings(db_session).settings_json = {
        "ai": {"instances": [{"id": "b", "api_key": NO_KEY}]}
    }
    db_session.commit()
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    secrets.require_key_if_secrets_stored(db_session)
