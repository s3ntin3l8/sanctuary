"""Local copy of every raw Gmail message Sanctuary fetches.

``DATA_DIR/gmail_raw/<user_id>/<gmail_id>.eml``, written *before* the message is
ingested (so a message whose ingest fails is kept too) and read before Gmail is
asked again. That makes a re-import after "Clear all data" — or after fixing a
parser bug — deterministic and offline: no Gmail traffic, no valid token needed.

This is real correspondence, so it stays on this machine (``data/`` is
gitignored), is created owner-only, is encrypted at rest with
``SECRETS_ENCRYPTION_KEY`` (messages and their metadata sidecars), and is never
used as a test fixture. It is
distinct from ``IngestBatch.raw_source_path`` on purpose: deleting a bundle must
not delete the cache.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path

from app import config as cfg
from app.core import secrets
from app.core.private_files import write_private

logger = logging.getLogger(__name__)

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


_undecryptable = 0
_undecryptable_lock = threading.Lock()


def take_undecryptable() -> int:
    """How many cached files could not be decrypted since the last call.

    With a rotated key that is every cached message, so a run logs this count
    once (``_read_plain`` itself stays at DEBUG) instead of one warning each."""
    global _undecryptable
    with _undecryptable_lock:
        count, _undecryptable = _undecryptable, 0
    return count


def log_undecryptable_summary(scope: str) -> None:
    """One WARNING per run for the cached mail that could not be decrypted (a
    rotated or wrong SECRETS_ENCRYPTION_KEY); those messages count as misses."""
    count = take_undecryptable()
    if count:
        logger.warning(
            "%s: %d cached message(s) could not be decrypted with the current "
            "SECRETS_ENCRYPTION_KEY and were treated as missing",
            scope,
            count,
        )


def _read_plain(path: Path) -> bytes | None:
    """The file's decrypted bytes. A file that cannot be read or decrypted
    (wrong or missing key) is a miss: the message is simply fetched again."""
    global _undecryptable
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return secrets.decrypt_bytes(data) or None
    except secrets.SecretsError:
        with _undecryptable_lock:
            _undecryptable += 1
        logger.debug(
            "Cached mail %s cannot be decrypted; treating as a miss", path.name
        )
        return None


def read(user_id: int, gmail_id: str) -> bytes | None:
    """The cached raw message, or None. An empty/unreadable file is a miss."""
    return _read_plain(_path(user_id, gmail_id))


def write(user_id: int, gmail_id: str, raw: bytes) -> None:
    """Store ``raw`` encrypted and atomically (temp file + rename), owner-only.
    Idempotent."""
    write_private(_path(user_id, gmail_id), secrets.encrypt_bytes(raw))


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
    """(message count, total bytes on disk — encrypted size) of this user's cache."""
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


def write_metadata(user_id: int, gmail_id: str, metadata: dict) -> None:
    path = _path(user_id, gmail_id).with_suffix(".json")
    write_private(path, secrets.encrypt_bytes(json.dumps(metadata).encode()))


def read_metadata(user_id: int, gmail_id: str) -> dict | None:
    data = _read_plain(_path(user_id, gmail_id).with_suffix(".json"))
    if data is None:
        return None
    try:
        value = json.loads(data)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
