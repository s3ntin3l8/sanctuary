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


def test_startup_fails_closed_without_a_key(monkeypatch):
    secrets.require_key()  # key present: fine
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with pytest.raises(secrets.SecretsError, match="SECRETS_ENCRYPTION_KEY"):
        secrets.require_key()


def test_the_celery_worker_refuses_to_start_without_a_key(monkeypatch):
    from app.tasks.celery_app import _require_encryption_key

    _require_encryption_key()
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with pytest.raises(secrets.SecretsError):
        _require_encryption_key()


def _seed_credentials(db_session, value):
    _admin_settings(db_session).settings_json = {"gmail_credentials_json": value}
    _app_settings(db_session).settings_json = {
        "ai": {
            "instances": [
                {"id": "a", "api_key": value and LIVE_KEY},
                {"id": "b", "api_key": NO_KEY},
            ]
        },
        "gmail_oauth": {
            "client_secret": value and "csecret"
        },  # pragma: allowlist secret
    }
    db_session.commit()


def test_the_sweep_encrypts_plaintext_credentials_and_leaves_the_placeholder(
    db_session,
):
    from app.services import reseal

    _seed_credentials(db_session, CREDS)
    assert reseal.probe(db_session)  # plaintext sample: a sweep is wanted
    report = reseal.reseal_db_secrets(db_session)
    assert report.encrypted == 3 and report.rotated == 0
    db_session.expire_all()
    user = _admin_settings(db_session).settings_json
    app = _app_settings(db_session).settings_json
    assert secrets.is_encrypted(user["gmail_credentials_json"])
    assert secrets.decrypt(user["gmail_credentials_json"]) == CREDS
    assert secrets.decrypt(app["ai"]["instances"][0]["api_key"]) == LIVE_KEY
    assert app["ai"]["instances"][1]["api_key"] == NO_KEY
    assert (
        secrets.decrypt(app["gmail_oauth"]["client_secret"]) == "csecret"
    )  # pragma: allowlist secret
    assert not reseal.reseal_db_secrets(db_session).changed
    assert not reseal.probe(db_session)  # settled: nothing to do at the next start


def test_the_sweep_rotates_credentials_off_a_retired_key(db_session, monkeypatch):
    from cryptography.fernet import Fernet

    from app.services import reseal

    _seed_credentials(db_session, secrets.encrypt(CREDS))
    _app_settings(db_session).settings_json = {
        "ai": {"instances": [{"id": "a", "api_key": secrets.encrypt(LIVE_KEY)}]}
    }
    db_session.commit()
    old = config.SECRETS_ENCRYPTION_KEY
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY_PREVIOUS", old)
    assert reseal.probe(db_session)  # a retired key is configured
    report = reseal.reseal_db_secrets(db_session)
    assert report.rotated == 2 and report.undecryptable == 0
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY_PREVIOUS", "")
    db_session.expire_all()
    user = _admin_settings(db_session).settings_json
    assert secrets.decrypt(user["gmail_credentials_json"]) == CREDS


def test_the_probe_names_the_missing_previous_key(db_session, monkeypatch, caplog):
    from cryptography.fernet import Fernet

    from app.services import reseal

    _seed_credentials(db_session, secrets.encrypt(CREDS))
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with caplog.at_level("ERROR", logger="app.services.reseal"):
        reseal.probe(db_session)
    assert any(
        "SECRETS_ENCRYPTION_KEY_PREVIOUS" in r.getMessage() for r in caplog.records
    )
    sealed = _admin_settings(db_session).settings_json["gmail_credentials_json"]
    report = reseal.reseal_db_secrets(db_session)
    # The credential nobody can open is counted and left exactly as it was.
    assert report.undecryptable == 1 and report.rotated == 0
    db_session.expire_all()
    assert _admin_settings(db_session).settings_json["gmail_credentials_json"] == sealed
