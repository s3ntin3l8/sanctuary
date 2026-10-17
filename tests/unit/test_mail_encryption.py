"""Raw mail on disk is encrypted at rest, including files written before that."""

import pytest
from cryptography.fernet import Fernet

from app import config
from app.core import secrets
from app.services import gmail_cache, mail_encryption

pytestmark = pytest.mark.unit

UID = 987654


@pytest.fixture(autouse=True)
def _clean():
    gmail_cache.clear(UID)
    yield
    gmail_cache.clear(UID)


def test_bytes_round_trip_and_legacy_plaintext_passes_through():
    token = secrets.encrypt_bytes(b"Subject: geheim")
    assert token.startswith(secrets.FILE_MAGIC) and b"geheim" not in token
    assert secrets.decrypt_bytes(token) == b"Subject: geheim"
    assert secrets.decrypt_bytes(b"plain old mail") == b"plain old mail"
    assert secrets.encrypt_bytes(token) == token  # never doubly encrypted


def test_a_wrong_key_raises_on_decrypt(monkeypatch):
    token = secrets.encrypt_bytes(b"x")
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with pytest.raises(secrets.SecretsError):
        secrets.decrypt_bytes(token)


def test_without_a_key_content_is_stored_as_is(monkeypatch):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    assert secrets.encrypt_bytes(b"x") == b"x"


def test_startup_pass_encrypts_existing_plaintext_files():
    directory = gmail_cache.cache_dir(UID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.eml").write_bytes(b"Subject: Klage\r\n\r\nplain")
    (directory / "a.json").write_text('{"subject": "Klage"}')
    sources = config.DATA_DIR / "email_sources" / str(UID)
    sources.mkdir(parents=True, exist_ok=True)
    (sources / "1.eml").write_bytes(b"Subject: Klage\r\n\r\nplain")
    try:
        assert mail_encryption.encrypt_existing() >= 3
        for path in (directory / "a.eml", directory / "a.json", sources / "1.eml"):
            assert b"Klage" not in path.read_bytes()
        assert gmail_cache.read(UID, "a") == b"Subject: Klage\r\n\r\nplain"
        assert gmail_cache.read_metadata(UID, "a") == {"subject": "Klage"}
        assert secrets.decrypt_bytes((sources / "1.eml").read_bytes()).endswith(
            b"plain"
        )
        assert mail_encryption.encrypt_existing() == 0  # idempotent
    finally:
        (sources / "1.eml").unlink(missing_ok=True)
        sources.rmdir()


def test_startup_pass_is_a_noop_without_a_key(monkeypatch):
    directory = gmail_cache.cache_dir(UID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.eml").write_bytes(b"plain")
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    assert mail_encryption.encrypt_existing() == 0
    assert (directory / "a.eml").read_bytes() == b"plain"
