"""Encrypt raw mail already on disk (written before it was encrypted at rest)."""

from __future__ import annotations

import logging
from pathlib import Path

from app import config
from app.core import secrets
from app.core.private_files import write_private

logger = logging.getLogger(__name__)


def _mail_files() -> list[Path]:
    raw = config.DATA_DIR / "gmail_raw"
    sources = config.DATA_DIR / "email_sources"
    return [
        *raw.glob("*/*.eml"),
        *raw.glob("*/*.json"),
        *sources.glob("*/*.eml"),
    ]


def encrypt_existing() -> int:
    """Rewrite plaintext Gmail cache files and stored .eml files encrypted.

    A no-op without ``SECRETS_ENCRYPTION_KEY``; already encrypted files are left
    alone, so it is cheap to run at every start. Returns how many were rewritten.
    """
    if not config.SECRETS_ENCRYPTION_KEY:
        return 0
    rewritten = 0
    for path in _mail_files():
        try:
            with path.open("rb") as handle:
                head = handle.read(len(secrets.FILE_MAGIC))
            if not head or secrets.is_encrypted_bytes(head):
                continue  # only the prefix is read: this runs at every start
            write_private(path, secrets.encrypt_bytes(path.read_bytes()))
            rewritten += 1
        except secrets.SecretsError:
            # An unusable key fails every file the same way: say so once and
            # leave the files alone rather than stop the app from starting.
            logger.warning(
                "Stored mail left unencrypted: the encryption key is unusable",
                exc_info=True,
            )
            break
        except OSError:
            logger.warning("Could not encrypt stored mail %s", path.name, exc_info=True)
    if rewritten:
        logger.info("Encrypted %d stored mail files at rest", rewritten)
    return rewritten
