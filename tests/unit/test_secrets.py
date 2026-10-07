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
    with pytest.raises(secrets.SecretsError, match="does not match"):
        secrets.decrypt(stored)


def test_missing_key_raises_with_instructions(monkeypatch):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with pytest.raises(secrets.SecretsError, match="SECRETS_ENCRYPTION_KEY"):
        secrets.encrypt("secret")


def test_malformed_key_raises(monkeypatch):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(secrets.SecretsError, match="valid Fernet key"):
        secrets.encrypt("secret")
