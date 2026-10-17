"""Encryption at rest for credentials stored in the settings JSON blobs and for
raw mail kept on disk.

Fernet (AES-128-CBC + HMAC) keyed by ``SECRETS_ENCRYPTION_KEY``, which is
mandatory (``require_key`` stops the web app and every worker without it).
``SECRETS_ENCRYPTION_KEY_PREVIOUS`` (comma-separated) lists retired keys that
still *open* old values; everything is *sealed* with the current key only, and
``reseal`` / ``reseal_bytes`` move a value onto it.

Encrypted values carry an ``enc:v1:`` prefix so reads can tell them from
plaintext: ``decrypt`` passes unprefixed values through unchanged (non-secret
sentinels such as the AI "not-needed" key), while a prefixed value that cannot
be decrypted raises ``SecretsError`` — a wrong or missing key must never
silently look like "no credentials".
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app import config

PREFIX = "enc:v1:"


class SecretsError(RuntimeError):
    """The encryption key is missing/wrong, so a stored secret is unusable."""


def _fernet_of(key: str, name: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise SecretsError(
            f"{name} is not a valid Fernet key (32 url-safe base64-encoded bytes)."
        ) from exc


@lru_cache(maxsize=8)
def _ring(key: str, previous: str) -> tuple[Fernet, MultiFernet]:
    current = _fernet_of(key, "SECRETS_ENCRYPTION_KEY")
    retired = [
        _fernet_of(k.strip(), "SECRETS_ENCRYPTION_KEY_PREVIOUS")
        for k in previous.split(",")
        if k.strip()
    ]
    return current, MultiFernet([current, *retired])


def _keys() -> tuple[Fernet, MultiFernet]:
    """(the current key alone, every key). Read per call: config is patched in tests."""
    key = config.SECRETS_ENCRYPTION_KEY
    if not key:
        raise SecretsError(
            "SECRETS_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'` and add it to .env."
        )
    return _ring(key, config.SECRETS_ENCRYPTION_KEY_PREVIOUS)


def require_key() -> None:
    """Fail closed: refuse to start without a usable key (also validates every
    retired key). Without one, mail and credentials would be written in the clear."""
    _keys()


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext``; already-encrypted values are returned as is."""
    if is_encrypted(plaintext):
        return plaintext
    return PREFIX + _keys()[0].encrypt(plaintext.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a prefixed value; return unprefixed values unchanged."""
    if not is_encrypted(value):
        return value
    try:
        return _keys()[1].decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise SecretsError(
            "Stored secret could not be decrypted — neither SECRETS_ENCRYPTION_KEY "
            "nor SECRETS_ENCRYPTION_KEY_PREVIOUS matches the key it was encrypted with."
        ) from exc


def reseal(value: str) -> str | None:
    """``value`` sealed with the current key if it was sealed with a retired one;
    ``None`` if it is not encrypted or already current. Raises ``SecretsError``
    if no key opens it."""
    if not is_encrypted(value):
        return None
    token = value[len(PREFIX) :].encode()
    current, ring = _keys()
    try:
        current.decrypt(token)
        return None
    except InvalidToken:
        pass
    try:
        return PREFIX + ring.rotate(token).decode()
    except InvalidToken as exc:
        raise SecretsError("Stored secret is sealed with an unknown key.") from exc


# Raw mail kept on disk (the Gmail cache and stored .eml files) is encrypted
# with the same key. A file starting with the magic is a Fernet token; anything
# else is legacy plaintext and is read as is until the startup pass rewrites it.
FILE_MAGIC = b"SNCENC1\n"


def is_encrypted_bytes(data: bytes) -> bool:
    return data.startswith(FILE_MAGIC)


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt file content with the current key (``SecretsError`` without one)."""
    # An empty file stays empty: the caches read that as a miss.
    if not data or is_encrypted_bytes(data):
        return data
    return FILE_MAGIC + _keys()[0].encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypt file content; legacy plaintext passes through unchanged."""
    if not is_encrypted_bytes(data):
        return data
    try:
        return _keys()[1].decrypt(data[len(FILE_MAGIC) :])
    except InvalidToken as exc:
        raise SecretsError(
            "Stored mail could not be decrypted — neither SECRETS_ENCRYPTION_KEY "
            "nor SECRETS_ENCRYPTION_KEY_PREVIOUS matches the key it was encrypted with."
        ) from exc


def reseal_bytes(data: bytes) -> bytes | None:
    """``data`` sealed with the current key if it was sealed with a retired one;
    ``None`` if it is plaintext or already current. Raises ``SecretsError`` if
    no key opens it."""
    if not is_encrypted_bytes(data):
        return None
    token = data[len(FILE_MAGIC) :]
    current, ring = _keys()
    try:
        current.decrypt(token)
        return None
    except InvalidToken:
        pass
    try:
        return FILE_MAGIC + ring.rotate(token)
    except InvalidToken as exc:
        raise SecretsError("Stored mail is sealed with an unknown key.") from exc
