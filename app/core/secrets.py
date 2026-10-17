"""Encryption at rest for credentials stored in the settings JSON blobs.

Fernet (AES-128-CBC + HMAC) keyed by ``SECRETS_ENCRYPTION_KEY``. Encrypted
values carry an ``enc:v1:`` prefix so reads can tell them from plaintext:
``decrypt`` passes unprefixed values through unchanged (non-secret sentinels
such as the AI "not-needed" key, and rows not yet migrated), while a prefixed
value that cannot be decrypted raises ``SecretsError`` — a wrong or missing key
must never silently look like "no credentials".
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app import config

PREFIX = "enc:v1:"


class SecretsError(RuntimeError):
    """The encryption key is missing/wrong, so a stored secret is unusable."""


def _fernet() -> Fernet:
    key = config.SECRETS_ENCRYPTION_KEY
    if not key:
        raise SecretsError(
            "SECRETS_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and add it to .env."
        )
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise SecretsError(
            "SECRETS_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe "
            "base64-encoded bytes)."
        ) from exc


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext``; already-encrypted values are returned as is."""
    if is_encrypted(plaintext):
        return plaintext
    return PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a prefixed value; return unprefixed values unchanged."""
    if not is_encrypted(value):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise SecretsError(
            "Stored secret could not be decrypted — SECRETS_ENCRYPTION_KEY "
            "does not match the key it was encrypted with."
        ) from exc


# Raw mail kept on disk (the Gmail cache and stored .eml files) is encrypted
# with the same key. A file starting with the magic is a Fernet token; anything
# else is legacy plaintext and is read as is until the startup pass rewrites it.
FILE_MAGIC = b"SNCENC1\n"


def is_encrypted_bytes(data: bytes) -> bool:
    return data.startswith(FILE_MAGIC)


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt file content; with no key configured it is stored as is (the
    same single-user trade-off as an unconfigured instance has everywhere)."""
    # An empty file stays empty: the caches read that as a miss.
    if not data or is_encrypted_bytes(data) or not config.SECRETS_ENCRYPTION_KEY:
        return data
    return FILE_MAGIC + _fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypt file content; legacy plaintext passes through unchanged."""
    if not is_encrypted_bytes(data):
        return data
    try:
        return _fernet().decrypt(data[len(FILE_MAGIC) :])
    except InvalidToken as exc:
        raise SecretsError(
            "Stored mail could not be decrypted — SECRETS_ENCRYPTION_KEY "
            "does not match the key it was encrypted with."
        ) from exc


def require_key_if_secrets_stored(db) -> None:
    """Fail closed at startup: encrypted credentials exist but no key is set.

    Without this the app would boot and then report every Gmail connection as
    "needs reconnect" and every AI key as unusable — hiding that the real fix
    is restoring SECRETS_ENCRYPTION_KEY (a reconnect with a *new* key would
    also orphan whatever the old key protected).
    """
    if config.SECRETS_ENCRYPTION_KEY:
        return
    from app.models.database import AppSettings, UserSettings

    stored: list[object] = []
    for row in db.query(UserSettings.settings_json).all():
        data = row[0] if isinstance(row[0], dict) else {}
        stored.append(data.get("gmail_credentials_json"))
    for row in db.query(AppSettings.settings_json).all():
        data = row[0] if isinstance(row[0], dict) else {}
        for inst in (data.get("ai") or {}).get("instances", []):
            stored.append(inst.get("api_key"))
        stored.append((data.get("gmail_oauth") or {}).get("client_secret"))
    if any(is_encrypted(v) for v in stored):
        _fernet()  # raises the "SECRETS_ENCRYPTION_KEY is not set" error
