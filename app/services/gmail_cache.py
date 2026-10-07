"""Local copy of every raw Gmail message Sanctuary fetches.

``DATA_DIR/gmail_raw/<user_id>/<gmail_id>.eml``, written *before* the message is
ingested (so a message whose ingest fails is kept too) and read before Gmail is
asked again. That makes a re-import after "Clear all data" — or after fixing a
parser bug — deterministic and offline: no Gmail traffic, no valid token needed.

This is real correspondence, so it stays on this machine (``data/`` is
gitignored), is created owner-only, and is never used as a test fixture. It is
distinct from ``IngestBatch.raw_source_path`` on purpose: deleting a bundle must
not delete the cache.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from app import config as cfg

CACHE_DIRNAME = "gmail_raw"

# Gmail message ids are lowercase hex. Anything else would be a path-traversal
# attempt, never a real id, so refuse it rather than try to sanitise.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def cache_dir(user_id: int) -> Path:
    return cfg.DATA_DIR / CACHE_DIRNAME / str(user_id)


def _path(user_id: int, gmail_id: str) -> Path:
    if not _ID_RE.fullmatch(gmail_id):
        raise ValueError(f"Not a Gmail message id: {gmail_id!r}")
    return cache_dir(user_id) / f"{gmail_id}.eml"


def read(user_id: int, gmail_id: str) -> bytes | None:
    """The cached raw message, or None. An empty/unreadable file is a miss."""
    try:
        raw = _path(user_id, gmail_id).read_bytes()
    except OSError:
        return None
    return raw or None


def write(user_id: int, gmail_id: str, raw: bytes) -> None:
    """Store ``raw`` atomically (temp file + rename), owner-only. Idempotent."""
    path = _path(user_id, gmail_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".eml")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cached_ids(user_id: int, gmail_ids: list[str]) -> set[str]:
    """Which of ``gmail_ids`` have a cached copy (one stat each, no reads)."""
    out = set()
    for gmail_id in gmail_ids:
        try:
            if _path(user_id, gmail_id).stat().st_size > 0:
                out.add(gmail_id)
        except (OSError, ValueError):
            continue
    return out


def stats(user_id: int) -> tuple[int, int]:
    """(message count, total bytes) of this user's cache."""
    count = size = 0
    try:
        for entry in os.scandir(cache_dir(user_id)):
            if entry.name.endswith(".eml") and entry.is_file():
                count += 1
                size += entry.stat().st_size
    except FileNotFoundError:
        pass
    return count, size


def clear(user_id: int) -> int:
    """Delete this user's cached messages (local files only). Returns how many."""
    count, _ = stats(user_id)
    shutil.rmtree(cache_dir(user_id), ignore_errors=True)
    return count
