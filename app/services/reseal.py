"""Keep stored secrets sealed with the current ``SECRETS_ENCRYPTION_KEY``.

Two kinds of data are app-encrypted: raw mail on disk (the Gmail cache and the
stored ``.eml`` files, ``SNCENC1`` files) and the credentials in the settings
JSON blobs (``enc:v1:`` values). ``reseal_all`` brings every one of them onto the
current key: plaintext is encrypted, anything sealed with a retired key
(``SECRETS_ENCRYPTION_KEY_PREVIOUS``) is re-encrypted, and what no key opens is
only counted. ``probe`` is the cheap startup check that decides whether the
sweep is worth running now.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app import config
from app.core import secrets
from app.core.private_files import write_private
from app.models.database import AppSettings, UserSettings

logger = logging.getLogger(__name__)

# The AI placeholder for keyless endpoints; not a secret and never encrypted.
_NO_KEY = "not-needed"


@dataclass
class ResealReport:
    encrypted: int = 0  # plaintext sealed for the first time
    rotated: int = 0  # moved from a retired key to the current one
    undecryptable: int = 0  # no configured key opens them; left untouched

    def __add__(self, other: ResealReport) -> ResealReport:
        return ResealReport(
            self.encrypted + other.encrypted,
            self.rotated + other.rotated,
            self.undecryptable + other.undecryptable,
        )

    @property
    def changed(self) -> bool:
        return bool(self.encrypted or self.rotated)


def _mail_files() -> list[Path]:
    raw = config.DATA_DIR / "gmail_raw"
    sources = config.DATA_DIR / "email_sources"
    return [
        *raw.glob("*/*.eml"),
        *raw.glob("*/*.json"),
        *sources.glob("*/*.eml"),
    ]


def reseal_mail() -> ResealReport:
    """Seal every stored mail file with the current key (see the module doc)."""
    report = ResealReport()
    for path in _mail_files():
        if path.name.startswith("."):
            continue  # a leftover temp file from an interrupted write
        try:
            data = path.read_bytes()
            if not data:
                continue
            # Deciding "already current" means decrypting the whole file: a daily
            # full read of the mailbox, traded for also counting what no key opens.
            was_encrypted = secrets.is_encrypted_bytes(data)
            resealed = (
                secrets.reseal_bytes(data)
                if was_encrypted
                else secrets.encrypt_bytes(data)
            )
            if resealed is None:
                continue
            write_private(path, resealed)  # counted only once it is on disk
            if was_encrypted:
                report.rotated += 1
            else:
                report.encrypted += 1
        except secrets.SecretsError:
            report.undecryptable += 1
        except OSError:
            logger.warning("Could not reseal stored mail %s", path.name, exc_info=True)
    return report


_SETTINGS_TABLES: list[tuple[type[UserSettings] | type[AppSettings], str]] = [
    (UserSettings, "user_settings"),
    (AppSettings, "app_settings"),
]


def _secret_slots(settings_json: dict, table: str) -> list[tuple[dict, str]]:
    """The (container, key) pairs holding credentials in a settings blob."""
    if table == "user_settings":
        return [(settings_json, "gmail_credentials_json")]
    slots = [
        (inst, "api_key")
        for inst in (settings_json.get("ai") or {}).get("instances", [])
        if isinstance(inst, dict)
    ]
    oauth = settings_json.get("gmail_oauth")
    if isinstance(oauth, dict):
        slots.append((oauth, "client_secret"))
    return slots


def reseal_db_secrets(db: Session) -> ResealReport:
    """Seal the credentials in ``user_settings`` / ``app_settings`` with the
    current key. Each row is locked while it is rewritten so a concurrent
    settings save is not overwritten."""
    report = ResealReport()
    for model, table in _SETTINGS_TABLES:
        for row_id in [r[0] for r in db.query(model.id).all()]:
            row = cast(
                "UserSettings | AppSettings",
                db.query(model).filter(model.id == row_id).with_for_update().one(),
            )
            data = row.settings_json if isinstance(row.settings_json, dict) else {}
            changed = False
            for container, key in _secret_slots(data, table):
                value = container.get(key)
                if not isinstance(value, str) or not value or value == _NO_KEY:
                    continue
                try:
                    if secrets.is_encrypted(value):
                        resealed = secrets.reseal(value)
                        if resealed is None:
                            continue
                        report.rotated += 1
                    else:
                        resealed = secrets.encrypt(value)
                        report.encrypted += 1
                except secrets.SecretsError:
                    report.undecryptable += 1
                    continue
                container[key] = resealed
                changed = True
            if changed:
                flag_modified(row, "settings_json")
            db.commit()  # releases the row lock either way
    return report


def reseal_all(db: Session) -> ResealReport:
    report = reseal_mail() + reseal_db_secrets(db)
    if report.changed or report.undecryptable:
        logger.log(
            logging.WARNING if report.undecryptable else logging.INFO,
            "Reseal: encrypted=%d rotated=%d undecryptable=%d",
            report.encrypted,
            report.rotated,
            report.undecryptable,
        )
    return report


def probe(db: Session) -> bool:
    """Startup check; True when a reseal sweep should run now.

    Looks at one stored mail file and one credential only. If a sample is sealed
    with a key that is not configured it logs a single clear error (the usual
    cause is a rotated key without ``SECRETS_ENCRYPTION_KEY_PREVIOUS``). A sweep
    is wanted when a retired key is configured or a sample is not sealed with the
    current key; the daily sweep catches everything else."""
    needed = bool(config.SECRETS_ENCRYPTION_KEY_PREVIOUS.strip())
    for path in _mail_files():
        if path.name.startswith("."):
            continue  # a leftover temp file, not mail
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data:
            continue
        try:
            needed = not secrets.is_encrypted_bytes(data) or needed
            secrets.reseal_bytes(data)
        except secrets.SecretsError:
            _log_key_mismatch("stored mail")
        break
    for model, table in _SETTINGS_TABLES:
        for (settings_json,) in db.query(model.settings_json):
            blob = settings_json if isinstance(settings_json, dict) else {}
            for container, key in _secret_slots(blob, table):
                value = container.get(key)
                if not isinstance(value, str) or not value or value == _NO_KEY:
                    continue
                try:
                    # Evaluated first: an unopenable credential must be logged
                    # even when a sweep is already due.
                    rotated = secrets.reseal(value) is not None
                    needed = rotated or not secrets.is_encrypted(value) or needed
                except secrets.SecretsError:
                    _log_key_mismatch("stored credentials")
                return needed
    return needed


def _log_key_mismatch(what: str) -> None:
    logger.error(
        "Some %s are sealed with a key that is not configured. If "
        "SECRETS_ENCRYPTION_KEY was changed, set SECRETS_ENCRYPTION_KEY_PREVIOUS "
        "to the old key so they can be read and moved onto the new one.",
        what,
    )
