"""Password hashing — argon2 via pwdlib, isolated behind a tiny interface.

Kept deliberately small and dependency-isolated so the underlying hasher can
be swapped (e.g. to bcrypt) without touching call sites. Only these functions
should be used elsewhere in the app.
"""

from __future__ import annotations

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

# argon2id with library defaults — sane, modern, memory-hard parameters.
_hasher = PasswordHash((Argon2Hasher(),))


def hash_password(password: str) -> str:
    """Return an argon2id hash string for the given plaintext password."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a plaintext against a stored hash.

    Returns False for missing hashes (e.g. OIDC-only accounts that have no
    local password) rather than raising, so callers can treat it as a plain
    auth failure.
    """
    if not password_hash:
        return False
    try:
        return _hasher.verify(password, password_hash)
    except Exception:
        return False


def verify_and_update(
    password: str, password_hash: str | None
) -> tuple[bool, str | None]:
    """Verify and, if the hash uses outdated parameters, return a fresh hash.

    Returns ``(valid, new_hash)``. ``new_hash`` is non-None only when the
    password was valid AND the stored hash should be upgraded — the login
    flow persists it in that case. Returns ``(False, None)`` for a missing or
    invalid hash.
    """
    if not password_hash:
        return (False, None)
    try:
        return _hasher.verify_and_update(password, password_hash)
    except Exception:
        return (False, None)
