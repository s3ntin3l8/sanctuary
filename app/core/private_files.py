"""Owner-only file writes: atomic (temp file + rename), 0600 in a 0700 directory."""

from __future__ import annotations

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
