"""Encrypted AI call dumps (``data/ai_debug/**/*.md``).

A dump holds the full prompt and response of an AI call, i.e. document text, so it
is only written while an admin has switched capture on, and never in the clear:
each recorded call is one line, ``enc:v1:<Fernet token>`` (``app.core.secrets``),
that decrypts to the call's markdown block. One line per call keeps appends cheap
(no rewriting the file) and lets the reader and the pruner work call by call.
``runs.jsonl`` (metadata only) is a separate, always-on index.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from app.core import secrets
from app.core.private_files import append_line_locked

logger = logging.getLogger(__name__)

UNREADABLE = (
    "[This call could not be decrypted: SECRETS_ENCRYPTION_KEY does not match the "
    "key it was recorded with.]"
)


def append_block(path: Path, block: str) -> None:
    """Record one call's markdown block, encrypted, as a single line."""
    append_line_locked(path, secrets.encrypt(block) + "\n")


def _plain(line: str) -> str:
    try:
        return secrets.decrypt(line)
    except secrets.SecretsError:
        return UNREADABLE


def read_blocks(text: str) -> list[tuple[str, str]]:
    """``(stored line, plaintext block)`` for every recorded call in ``text``.
    Anything that is not an encrypted line (e.g. a plaintext dump from before
    capture was encrypted) is ignored."""
    return [
        (line, _plain(line))
        for line in text.splitlines()
        if secrets.is_encrypted(line.strip())
    ]


def read_text(path: Path) -> str:
    """The file's calls decrypted and joined, oldest first."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(plain for _line, plain in read_blocks(text))


def purge_plaintext(root: Path) -> int:
    """Delete dumps written before capture was encrypted (any ``.md`` without a
    single encrypted call line), and the directories that leaves empty. Returns
    how many files were removed."""
    removed = 0
    if not root.is_dir():
        return removed
    for path in root.rglob("*.md"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if any(secrets.is_encrypted(ln.strip()) for ln in lines):
                continue  # a call is encrypted: at worst a torn append, keep it
            path.unlink()
            removed += 1
            with contextlib.suppress(OSError):
                path.parent.rmdir()  # only succeeds when nothing else is in it
        except OSError:
            logger.warning("Could not check AI debug dump %s", path.name, exc_info=True)
    return removed
