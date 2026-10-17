"""Raw mail on disk is encrypted at rest, including files written before that."""

import pytest
from cryptography.fernet import Fernet

from app import config
from app.core import secrets
from app.services import gmail_cache, reseal

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


def test_encrypting_without_a_key_is_an_error_not_plaintext(monkeypatch):
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY", "")
    with pytest.raises(secrets.SecretsError):
        secrets.encrypt_bytes(b"x")


def test_the_sweep_encrypts_existing_plaintext_files():
    directory = gmail_cache.cache_dir(UID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.eml").write_bytes(b"Subject: Klage\r\n\r\nplain")
    (directory / "a.json").write_text('{"subject": "Klage"}')
    sources = config.DATA_DIR / "email_sources" / str(UID)
    sources.mkdir(parents=True, exist_ok=True)
    (sources / "1.eml").write_bytes(b"Subject: Klage\r\n\r\nplain")
    try:
        assert reseal.reseal_mail().encrypted >= 3
        for path in (directory / "a.eml", directory / "a.json", sources / "1.eml"):
            assert b"Klage" not in path.read_bytes()
        assert gmail_cache.read(UID, "a") == b"Subject: Klage\r\n\r\nplain"
        assert gmail_cache.read_metadata(UID, "a") == {"subject": "Klage"}
        assert secrets.decrypt_bytes((sources / "1.eml").read_bytes()).endswith(
            b"plain"
        )
        assert not reseal.reseal_mail().changed  # idempotent
    finally:
        (sources / "1.eml").unlink(missing_ok=True)
        sources.rmdir()


def test_the_sweep_moves_files_off_a_retired_key(monkeypatch):
    gmail_cache.write(UID, "a", b"Subject: Klage")
    gmail_cache.write_metadata(UID, "a", {"subject": "Klage"})
    old = config.SECRETS_ENCRYPTION_KEY
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY_PREVIOUS", old)
    assert gmail_cache.read(UID, "a") == b"Subject: Klage"  # retired key still opens
    report = reseal.reseal_mail()
    assert report.rotated >= 2 and report.undecryptable == 0
    monkeypatch.setattr(config, "SECRETS_ENCRYPTION_KEY_PREVIOUS", "")
    assert gmail_cache.read(UID, "a") == b"Subject: Klage"
    assert gmail_cache.read_metadata(UID, "a") == {"subject": "Klage"}
    assert not reseal.reseal_mail().changed


def test_the_sweep_counts_files_no_key_opens_and_leaves_them_alone(monkeypatch):
    gmail_cache.write(UID, "a", b"Subject: Klage")
    path = gmail_cache.cache_dir(UID) / "a.eml"
    before = path.read_bytes()
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    report = reseal.reseal_mail()
    assert report.undecryptable >= 1 and not report.changed
    assert path.read_bytes() == before


def test_the_sweep_ignores_leftover_temp_files():
    directory = gmail_cache.cache_dir(UID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".tmp-abc.eml").write_bytes(b"half written")
    assert reseal.reseal_mail().encrypted == 0
    assert (directory / ".tmp-abc.eml").read_bytes() == b"half written"


def test_the_probe_skips_leftover_temp_files(db_session):
    directory = gmail_cache.cache_dir(UID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".tmp-abc.eml").write_bytes(b"half written")
    gmail_cache.write(UID, "a", b"Subject: Klage")
    # The leftover must not make every start look like it needs a sweep.
    assert not reseal.probe(db_session)


def test_a_failed_write_is_not_counted(monkeypatch):
    directory = gmail_cache.cache_dir(UID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a.eml").write_bytes(b"plain")

    def fail(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(reseal, "write_private", fail)
    assert not reseal.reseal_mail().changed
    assert (directory / "a.eml").read_bytes() == b"plain"
