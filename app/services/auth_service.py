"""Account/session helpers shared by the auth gate, the dependencies, and the
auth routes. Keeps session-validation logic in one place so the ASGI gate and
the `get_current_user` dependency can never drift apart.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import config
from app.core import security
from app.models.database import User, UserSettings
from app.models.enums import UserRole

logger = logging.getLogger(__name__)


def bootstrap_admin_id(db: Session) -> int | None:
    """The id of the designated primary admin, pinned in AppSettings.

    Identity is pinned by id (never by email), so renaming the admin's email
    never orphans ownership or spawns a duplicate admin.
    """
    from app.models.database import AppSettings

    row = db.query(AppSettings).first()
    if row and isinstance(row.settings_json, dict):
        val = row.settings_json.get("bootstrap_admin_id")
        if isinstance(val, int):
            return val
    return None


def set_bootstrap_admin_id(db: Session, user_id: int) -> None:
    from app.models.database import AppSettings

    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(settings_json={})
        db.add(row)
        db.flush()
    data = dict(row.settings_json or {})
    data["bootstrap_admin_id"] = int(user_id)
    row.settings_json = data
    db.flush()


def count_users(db: Session) -> int:
    return db.query(User).count()


def get_user_by_email(db: Session, email: str) -> User | None:
    if not email:
        return None
    return db.query(User).filter(User.email == email.strip().lower()).first()


def _ensure_admin_scaffold(db: Session, user: User) -> None:
    """(Re)assert admin role + active state and the per-user scaffolding."""
    if user.role != UserRole.ADMIN:
        user.role = UserRole.ADMIN
    if not user.is_active:
        user.is_active = True
    ensure_username(db, user)
    ensure_user_settings(db, user)
    ensure_user_scan_dir(user)


def get_or_create_bootstrap_admin(db: Session) -> User | None:
    """Resolve the designated primary admin, or None if the app isn't set up yet.

    Identity is pinned by id, never re-derived from an email string. Resolution:
      1. The admin pinned by id in AppSettings (stable across email renames).
      2. Backfill: the earliest existing admin — pins it for next time. Covers
         upgrades and the account just made on the first-run create-admin screen.
      3. Seed from BOOTSTRAP_ADMIN_EMAIL + BOOTSTRAP_ADMIN_PASSWORD, but only on a
         truly fresh DB (no users) — the optional code-driven provisioning path
         (e.g. Ansible). Both must be set; otherwise the screen onboards instead.
      4. None — no admin exists; the caller must route to the create-admin screen.
    """
    pinned = bootstrap_admin_id(db)
    if pinned is not None:
        user = db.get(User, pinned)
        if user is not None and user.is_active:
            _ensure_admin_scaffold(db, user)
            return user

    existing_admin = (
        db.query(User)
        .filter(User.role == UserRole.ADMIN, User.is_active.is_(True))
        .order_by(User.id)
        .first()
    )
    if existing_admin is not None:
        set_bootstrap_admin_id(db, existing_admin.id)
        _ensure_admin_scaffold(db, existing_admin)
        return existing_admin

    if (
        config.BOOTSTRAP_ADMIN_EMAIL
        and config.BOOTSTRAP_ADMIN_PASSWORD
        and count_users(db) == 0
    ):
        user = create_user(
            db,
            email=config.BOOTSTRAP_ADMIN_EMAIL,
            password=config.BOOTSTRAP_ADMIN_PASSWORD,
            role=UserRole.ADMIN,
            display_name="Administrator",
        )
        set_bootstrap_admin_id(db, user.id)
        return user

    return None


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s or "user"


def ensure_username(db: Session, user: User) -> str:
    """Assign a unique URL/filesystem-safe username slug if the user lacks one.

    Derived from display_name, else the email local-part; deduped with a numeric
    suffix. Names the user's scan-ingest subfolder.
    """
    if user.username:
        return user.username
    base = _slugify(user.display_name or (user.email or "").split("@", 1)[0])
    candidate = base
    n = 2
    while (
        db.query(User).filter(User.username == candidate, User.id != user.id).first()
        is not None
    ):
        candidate = f"{base}-{n}"
        n += 1
    user.username = candidate
    db.flush()
    return candidate


def user_scan_dir(user: User) -> Path:
    """The per-user scan-ingest incoming subfolder for this user."""
    return config.SCAN_INCOMING_DIR / (user.username or f"user-{user.id}")


def ensure_user_scan_dir(user: User) -> None:
    """Create the user's incoming scan subfolder (best-effort)."""
    try:
        user_scan_dir(user).mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - filesystem edge
        logger.warning("Could not create scan dir for user %s: %s", user.id, exc)


def ensure_user_settings(db: Session, user: User) -> UserSettings:
    """Get-or-create the per-user settings row."""
    settings = (
        db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
        if user.id is not None
        else None
    )
    if settings is None:
        settings = UserSettings(user_id=user.id)
        db.add(settings)
        db.flush()
    return settings


class EmailAlreadyExists(Exception):
    """Raised when creating a user with an email that is already registered."""


class InvalidEmail(Exception):
    """Raised when an email address fails basic format validation."""


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


def create_user(
    db: Session,
    *,
    email: str,
    password: str | None,
    role: UserRole = UserRole.USER,
    display_name: str | None = None,
) -> User:
    """Create a new account. Raises EmailAlreadyExists on a duplicate email."""
    email = (email or "").strip().lower()
    if get_user_by_email(db, email) is not None:
        raise EmailAlreadyExists(email)
    user = User(
        email=email,
        password_hash=security.hash_password(password) if password else None,
        display_name=display_name or None,
        role=role,
        is_active=True,
    )
    db.add(user)
    db.flush()
    ensure_username(db, user)
    ensure_user_settings(db, user)
    ensure_user_scan_dir(user)
    return user


def change_email(db: Session, user: User, new_email: str) -> None:
    """Rename a user's login email in place.

    Ownership FKs reference users.id (never the email string), and the bootstrap
    admin is pinned by id — so a rename never reassigns documents nor orphans the
    account. Raises InvalidEmail on a malformed address and EmailAlreadyExists if
    another account already uses it.
    """
    normalized = (new_email or "").strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise InvalidEmail(normalized)
    if normalized == (user.email or "").strip().lower():
        return  # no-op rename
    existing = get_user_by_email(db, normalized)
    if existing is not None and existing.id != user.id:
        raise EmailAlreadyExists(normalized)
    user.email = normalized
    db.flush()


def set_password(db: Session, user: User, password: str) -> None:
    """Set a new password and invalidate all existing sessions for the user."""
    user.password_hash = security.hash_password(password)
    bump_token_version(user)


def bump_token_version(user: User) -> None:
    """Invalidate every outstanding signed-cookie session for this user."""
    user.token_version = (user.token_version or 0) + 1


def signup_enabled(db: Session) -> bool:
    """Whether self-service signup is allowed.

    A runtime override stored in AppSettings (set from the admin UI) wins over
    the AUTH_SIGNUP_ENABLED env default, so signup can be toggled without a
    restart.
    """
    from app.models.database import AppSettings

    row = db.query(AppSettings).first()
    if (
        row
        and isinstance(row.settings_json, dict)
        and "auth_signup_enabled" in row.settings_json
    ):
        return bool(row.settings_json["auth_signup_enabled"])
    return config.AUTH_SIGNUP_ENABLED


def set_signup_enabled(db: Session, value: bool) -> None:
    from app.models.database import AppSettings

    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(settings_json={})
        db.add(row)
        db.flush()
    data = dict(row.settings_json or {})
    data["auth_signup_enabled"] = bool(value)
    row.settings_json = data
    db.flush()


def link_or_create_oidc_user(
    db: Session,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    display_name: str | None,
    signup_allowed: bool,
) -> User | None:
    """Resolve an OIDC identity to a local user (Phase 2 account linking).

    1. Match on (oidc_issuer, oidc_subject) → existing linked account.
    2. Else match on verified email → link it (store issuer/subject).
    3. Else create a new regular user IF signup is allowed; otherwise None.
    """
    subject = (subject or "").strip()
    email = (email or "").strip().lower() or None

    linked = (
        db.query(User)
        .filter(User.oidc_issuer == issuer, User.oidc_subject == subject)
        .first()
    )
    if linked is not None:
        return linked if linked.is_active else None

    if email:
        by_email = get_user_by_email(db, email)
        if by_email is not None:
            if not by_email.is_active:
                return None
            by_email.oidc_issuer = issuer
            by_email.oidc_subject = subject
            if not by_email.display_name and display_name:
                by_email.display_name = display_name
            db.flush()
            return by_email

    if not signup_allowed or not email:
        return None

    user = User(
        email=email,
        password_hash=None,  # OIDC-only account
        display_name=display_name or None,
        role=UserRole.USER,
        is_active=True,
        oidc_issuer=issuer,
        oidc_subject=subject,
    )
    db.add(user)
    db.flush()
    ensure_username(db, user)
    ensure_user_settings(db, user)
    ensure_user_scan_dir(user)
    return user


def build_session(user: User) -> dict:
    """Construct the signed-cookie session payload for a logged-in user."""
    return {
        "uid": user.id,
        "tv": user.token_version,
        "iat": datetime.now(UTC).isoformat(),
    }


def resolve_session_user(db: Session, session: dict | None) -> User | None:
    """Return the valid, active user for a session payload, or None.

    Rejects: missing uid, unknown/inactive user, token_version mismatch
    (revoked session), or a session older than SESSION_LIFETIME_SECONDS.
    """
    if not session:
        return None
    uid = session.get("uid")
    if not isinstance(uid, int):
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        return None
    if session.get("tv") != user.token_version:
        return None
    iat_raw = session.get("iat")
    if not _session_is_fresh(iat_raw):
        return None
    return user


def _session_is_fresh(iat_raw) -> bool:
    if not iat_raw:
        return False
    try:
        iat = datetime.fromisoformat(iat_raw)
    except (TypeError, ValueError):
        return False
    if iat.tzinfo is None:
        iat = iat.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - iat).total_seconds()
    return 0 <= age <= config.SESSION_LIFETIME_SECONDS
