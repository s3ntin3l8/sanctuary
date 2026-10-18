"""write_private: atomic, owner-only and durable (the rename itself is fsynced)."""

import fcntl
import os
import stat
from unittest.mock import patch

import pytest

from app.core import private_files
from app.core.private_files import append_line_locked, write_private


def test_writes_owner_only_in_a_private_directory(tmp_path):
    target = tmp_path / "new" / "mail.eml"
    write_private(target, b"payload")
    assert target.read_bytes() == b"payload"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_replaces_an_existing_file_without_leaving_a_temp_file(tmp_path):
    target = tmp_path / "mail" / "mail.eml"
    write_private(target, b"old")
    write_private(target, b"new")
    assert target.read_bytes() == b"new"
    assert [p.name for p in target.parent.iterdir()] == ["mail.eml"]


def test_a_failed_write_keeps_the_old_file_and_removes_the_temp_file(tmp_path):
    target = tmp_path / "mail" / "mail.eml"
    write_private(target, b"old")
    with patch("os.replace", side_effect=OSError("disk gone")):
        with pytest.raises(OSError):
            write_private(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in target.parent.iterdir()] == ["mail.eml"]


def test_the_parent_directory_is_fsynced_after_the_rename(tmp_path):
    target = tmp_path / "mail" / "mail.eml"
    order: list[str] = []
    real_replace, real_fsync_dir = os.replace, private_files._fsync_dir

    def replace(src, dst):
        order.append("replace")
        real_replace(src, dst)

    def fsync_dir(directory):
        order.append(f"fsync_dir:{directory.name}")
        real_fsync_dir(directory)

    with (
        patch("os.replace", replace),
        patch.object(private_files, "_fsync_dir", fsync_dir),
    ):
        write_private(target, b"payload")
    assert order == ["replace", "fsync_dir:mail"]


def test_a_filesystem_that_cannot_fsync_a_directory_does_not_fail_the_write(tmp_path):
    target = tmp_path / "mail" / "mail.eml"
    real_open = os.open

    def open_dir(path, flags, *args, **kwargs):
        if flags & os.O_DIRECTORY:
            raise OSError("fsync on a directory is not supported")
        return real_open(path, flags, *args, **kwargs)

    with patch("os.open", open_dir):
        write_private(target, b"payload")
    assert target.read_bytes() == b"payload"


def _replace_on_first_lock(monkeypatch, action):
    """Run ``action`` the first time the appender takes the lock, i.e. after it
    opened the file and before it writes (the window a pruner can hit)."""
    real_flock, fired = fcntl.flock, []

    def flock(fd, op):
        real_flock(fd, op)
        if not fired:
            fired.append(1)
            action()

    monkeypatch.setattr(fcntl, "flock", flock)


def test_an_append_survives_the_file_being_rewritten_under_it(tmp_path, monkeypatch):
    target = tmp_path / "dump.md"
    append_line_locked(target, "old\n")
    _replace_on_first_lock(monkeypatch, lambda: write_private(target, b"kept\n"))
    append_line_locked(target, "new\n")
    assert target.read_text() == "kept\nnew\n"


def test_an_append_survives_the_file_being_pruned_away(tmp_path, monkeypatch):
    target = tmp_path / "dump.md"
    append_line_locked(target, "old\n")
    _replace_on_first_lock(monkeypatch, lambda: target.unlink())
    append_line_locked(target, "new\n")
    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_an_append_gives_up_when_the_file_never_stays_put(tmp_path, monkeypatch):
    target = tmp_path / "dump.md"
    append_line_locked(target, "old\n")
    real_flock = fcntl.flock

    def flock(fd, op):
        real_flock(fd, op)
        write_private(target, b"churn\n")

    monkeypatch.setattr(fcntl, "flock", flock)
    with pytest.raises(OSError):
        append_line_locked(target, "new\n")


def test_an_existing_file_with_loose_permissions_is_tightened(tmp_path):
    target = tmp_path / "runs.jsonl"
    target.write_text("old\n")
    target.chmod(0o644)
    append_line_locked(target, "new\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_temp_file_never_looks_like_the_file_being_written(tmp_path):
    """A pass globbing ``*.md`` / ``*.jsonl`` must not pick up a live temp file."""
    target = tmp_path / "doc.md"
    seen: list[str] = []
    real_replace = os.replace

    def replace(src, dst):
        seen.append(os.path.basename(src))
        real_replace(src, dst)

    with patch("os.replace", replace):
        write_private(target, b"x")
    assert seen and not seen[0].endswith((".md", ".jsonl", ".eml"))
