"""Encryption at rest for stored credentials (app/core/secrets.py)."""

import pytest
from cryptography.fernet import Fernet

from app import config
from app.core import secrets

pytestmark = pytest.mark.unit


def test_round_trip():
    stored = secrets.encrypt('{"refresh_token": "abc"}')
    assert secrets.decrypt(stored) == '{"refresh_token": "abc"}'


def test_stored_value_is_prefixed_and_not_plaintext():
    stored = secrets.encrypt("hunter2")  # pragma: allowlist secret
    assert stored.startswith(secrets.PREFIX)
    assert "hunter2" not in stored  # pragma: allowlist secret


def test_encrypt_is_idempotent_on_encrypted_values():
    once = secrets.encrypt("x")
    assert secrets.encrypt(once) == once


def test_decrypt_passes_plaintext_through():
    # Non-secret sentinels (AI "not-needed") and not-yet-migrated rows.
    assert secrets.decrypt("not-needed") == "not-needed"


def test_wrong_key_raises_instead_of_looking_like_no_credentials(monkeypatch):
    stored = secrets.encrypt("secret")
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with pytest.raises(secrets.SecretsError, match="SECRETS_ENCRYPTION_KEY"):
        secrets.decrypt(stored)


def test_missing_key_raises_with_instructions(monkeypatch):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with pytest.raises(secrets.SecretsError, match="SECRETS_ENCRYPTION_KEY"):
        secrets.encrypt("secret")


def test_malformed_key_raises(monkeypatch):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(secrets.SecretsError, match="valid Fernet key"):
        secrets.encrypt("secret")


def test_require_key_refuses_a_missing_or_malformed_key(monkeypatch):
    secrets.require_key()  # the test key is valid
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with pytest.raises(secrets.SecretsError, match="not set"):
        secrets.require_key()
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(secrets.SecretsError, match="valid Fernet key"):
        secrets.require_key()


def test_require_key_also_validates_retired_keys(monkeypatch):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY_PREVIOUS", "garbage")
    with pytest.raises(secrets.SecretsError, match="SECRETS_ENCRYPTION_KEY_PREVIOUS"):
        secrets.require_key()


def _rotate_to_new_key(monkeypatch) -> str:
    """Make the current key retired and return a new current key."""
    old = config.SECRETS_ENCRYPTION_KEY
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY_PREVIOUS", old)
    return old


def test_a_retired_key_still_opens_old_values_and_reseal_moves_them(monkeypatch):
    stored = secrets.encrypt("secret")
    old = _rotate_to_new_key(monkeypatch)
    assert secrets.decrypt(stored) == "secret"
    resealed = secrets.reseal(stored)
    assert resealed is not None and resealed != stored
    assert resealed.startswith(secrets.PREFIX)
    assert secrets.reseal(resealed) is None  # already on the current key
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY_PREVIOUS", "")
    assert secrets.decrypt(resealed) == "secret"  # readable without the old key
    with pytest.raises(secrets.SecretsError):
        secrets.decrypt(stored)
    assert old  # the old key was only ever needed for the rotation


def test_bytes_follow_the_same_rotation(monkeypatch):
    token = secrets.encrypt_bytes(b"Subject: geheim")
    _rotate_to_new_key(monkeypatch)
    assert secrets.decrypt_bytes(token) == b"Subject: geheim"
    resealed = secrets.reseal_bytes(token)
    assert resealed is not None and resealed.startswith(secrets.FILE_MAGIC)
    assert secrets.reseal_bytes(resealed) is None
    assert secrets.reseal_bytes(b"plaintext") is None
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY_PREVIOUS", "")
    assert secrets.decrypt_bytes(resealed) == b"Subject: geheim"


def test_reseal_raises_when_no_configured_key_opens_the_value(monkeypatch):
    stored = secrets.encrypt("secret")
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with pytest.raises(secrets.SecretsError):
        secrets.reseal(stored)
