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
    assert gmail_cache.stats(UID) == (2, 5)

    assert gmail_cache.clear(UID) == 2
    assert gmail_cache.stats(UID) == (0, 0)
    assert gmail_cache.clear(UID) == 0  # nothing left is fine


def test_users_never_see_each_others_mail():
    gmail_cache.write(UID, "m1", b"mine")
    assert gmail_cache.read(UID + 1, "m1") is None
    gmail_cache.write(UID + 1, "m1", b"theirs")
    gmail_cache.clear(UID)
    assert gmail_cache.read(UID + 1, "m1") == b"theirs"
