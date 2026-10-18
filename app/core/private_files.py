"""Owner-only file writes: atomic (temp file + rename), 0600 in a 0700 directory."""

from __future__ import annotations

import fcntl
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            # Stored mail cannot always be re-fetched: a crash after the rename
            # must not leave a zero-length file in place of the message.
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Make the rename itself durable: the new directory entry is only on disk
    once the parent directory is flushed."""
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Some network/overlay filesystems cannot fsync a directory. The file's
        # own fsync is the real guarantee and the data is already in place, so
        # this must not fail an otherwise successful write.
        logger.debug("Could not fsync directory %s", directory, exc_info=True)


_APPEND_ATTEMPTS = 5


def is_current(fd: int, path: Path) -> bool:
    """Whether ``path`` still names the file ``fd`` refers to. A holder of the
    lock of a file that was replaced or deleted meanwhile has a stale view."""
    try:
        current = os.stat(path)
    except FileNotFoundError:
        return False
    locked = os.fstat(fd)
    return (current.st_dev, current.st_ino) == (locked.st_dev, locked.st_ino)


def append_line_locked(path: Path, line: str) -> None:
    """Append ``line`` to ``path`` (0600, directory 0700) under an exclusive lock.

    A pruner rewrites such files with a temp file + rename, which leaves a
    concurrent appender holding the lock of the replaced (or deleted) inode:
    its write would go to a file nobody can reach. So after taking the lock the
    path is checked to still be the file that was locked, and the append starts
    over otherwise.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(_APPEND_ATTEMPTS):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not is_current(fd, path):
                continue
            os.fchmod(fd, 0o600)  # mode above only applies to a new file
            data = memoryview(line.encode("utf-8"))
            while data:
                data = data[os.write(fd, data) :]
            return
        finally:
            os.close(fd)  # releases the lock
    raise OSError(f"{path.name} kept being replaced while appending")
