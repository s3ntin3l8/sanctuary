"""Local raw-message cache (app/services/gmail_cache.py)."""

import os
import stat
from unittest.mock import patch

import pytest

from app.services import gmail_cache

pytestmark = pytest.mark.unit

UID = 42


@pytest.fixture(autouse=True)
def _clean():
    gmail_cache.clear(UID)
    gmail_cache.clear(UID + 1)
    yield
    gmail_cache.clear(UID)
    gmail_cache.clear(UID + 1)


def test_round_trip():
    gmail_cache.write(UID, "18c3f0a9", b"From: a\r\n\r\nbody")
    assert gmail_cache.read(UID, "18c3f0a9") == b"From: a\r\n\r\nbody"


def test_a_miss_is_none():
    assert gmail_cache.read(UID, "nope") is None


def test_writes_are_idempotent_and_replace_in_full():
    gmail_cache.write(UID, "m1", b"first version, longer")
    gmail_cache.write(UID, "m1", b"second")
    assert gmail_cache.read(UID, "m1") == b"second"


def test_an_empty_file_is_a_miss_not_an_empty_email():
    gmail_cache.write(UID, "m1", b"")
    assert gmail_cache.read(UID, "m1") is None
    assert gmail_cache.cached_ids(UID, ["m1"]) == set()


def test_files_and_directory_are_owner_only():
    gmail_cache.write(UID, "m1", b"x")
    directory = gmail_cache.cache_dir(UID)
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(directory / "m1.eml").st_mode) == 0o600


def test_a_failed_write_leaves_neither_a_partial_file_nor_a_temp_file():
    gmail_cache.write(UID, "m1", b"original")
    with patch.object(gmail_cache.os, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            gmail_cache.write(UID, "m1", b"new")
    assert gmail_cache.read(UID, "m1") == b"original"  # untouched
    assert sorted(p.name for p in gmail_cache.cache_dir(UID).iterdir()) == ["m1.eml"]


@pytest.mark.parametrize(
    "bad", ["../escape", "a/b", "..", "", "x" * 200, "with space", "a\x00b"]
)
def test_ids_that_could_escape_the_directory_are_refused(bad):
    with pytest.raises(ValueError):
        gmail_cache.write(UID, bad, b"x")
    with pytest.raises(ValueError):
        gmail_cache.read(UID, bad)


def test_cached_ids_checks_without_reading_and_ignores_garbage():
    gmail_cache.write(UID, "a", b"1")
    gmail_cache.write(UID, "b", b"22")
    assert gmail_cache.cached_ids(UID, ["a", "b", "c", "../x"]) == {"a", "b"}


def test_stats_and_clear():
    assert gmail_cache.stats(UID) == (0, 0)
    gmail_cache.write(UID, "a", b"123")
    gmail_cache.write(UID, "b", b"45")
    count, size = gmail_cache.stats(UID)
    assert count == 2
    assert size > 5  # the bytes on disk, i.e. the encrypted size

    assert gmail_cache.clear(UID) == 2
    assert gmail_cache.stats(UID) == (0, 0)
    assert gmail_cache.clear(UID) == 0  # nothing left is fine


def test_users_never_see_each_others_mail():
    gmail_cache.write(UID, "m1", b"mine")
    assert gmail_cache.read(UID + 1, "m1") is None
    gmail_cache.write(UID + 1, "m1", b"theirs")
    gmail_cache.clear(UID)
    assert gmail_cache.read(UID + 1, "m1") == b"theirs"


def _on_disk(gmail_id, suffix=".eml"):
    return (gmail_cache.cache_dir(UID) / f"{gmail_id}{suffix}").read_bytes()


def test_the_message_is_encrypted_on_disk():
    gmail_cache.write(UID, "m1", b"Subject: Klage gegen Mustermann\r\n\r\nbody")
    on_disk = _on_disk("m1")
    assert b"Mustermann" not in on_disk
    assert on_disk.startswith(b"SNCENC1\n")
    assert gmail_cache.read(UID, "m1") == b"Subject: Klage gegen Mustermann\r\n\r\nbody"


def test_the_metadata_sidecar_is_encrypted_on_disk():
    gmail_cache.write_metadata(UID, "m1", {"subject": "Klage gegen Mustermann"})
    assert b"Mustermann" not in _on_disk("m1", ".json")
    assert gmail_cache.read_metadata(UID, "m1") == {"subject": "Klage gegen Mustermann"}


def test_legacy_plaintext_files_are_still_read():
    gmail_cache.write(UID, "m1", b"x")
    (gmail_cache.cache_dir(UID) / "m1.eml").write_bytes(b"From: old\r\n\r\nplain")
    (gmail_cache.cache_dir(UID) / "m1.json").write_text('{"subject": "old"}')
    assert gmail_cache.read(UID, "m1") == b"From: old\r\n\r\nplain"
    assert gmail_cache.read_metadata(UID, "m1") == {"subject": "old"}


def test_a_wrong_key_is_a_cache_miss_not_an_error(monkeypatch):
    from cryptography.fernet import Fernet

    from app import config

    gmail_cache.write(UID, "m1", b"secret mail")
    gmail_cache.write_metadata(UID, "m1", {"subject": "s"})
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    assert gmail_cache.read(UID, "m1") is None
    assert gmail_cache.read_metadata(UID, "m1") is None
    assert gmail_cache.cached_ids(UID, ["m1"]) == {"m1"}  # present, just unreadable
