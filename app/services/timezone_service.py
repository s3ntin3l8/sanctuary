"""User-configurable timezone provider.

Single source of truth for the display timezone. Reads UserSettings from the
DB (UI override) and falls back to the TIMEZONE env var. Result is cached
in-process; call invalidate() after any UI write to reset it.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, available_timezones

logger = logging.getLogger(__name__)

CURATED_ZONES = [
    "UTC",
    "Europe/Berlin",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
    "Asia/Tokyo",
]

_UTC = ZoneInfo("UTC")
_cached_tz: ZoneInfo | None = None
_loading: bool = False  # re-entrance guard during DB read


def _validated_zone(name: str) -> ZoneInfo:
    if name == "UTC" or name in available_timezones():
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    logger.warning("Invalid TIMEZONE %r — falling back to UTC", name)
    return _UTC


def _read_from_db() -> str | None:
    """Return the timezone string stored in AppSettings, or None."""
    try:
        from app.config import SessionLocal
        from app.models.database import AppSettings

        db = SessionLocal()
        try:
            settings = db.query(AppSettings).first()
            if settings and settings.settings_json:
                return settings.settings_json.get("timezone")
        finally:
            db.close()
    except Exception:
        pass
    return None


def get_user_tz() -> ZoneInfo:
    """Return the active timezone (cached). Thread-safe for single-process use."""
    global _cached_tz, _loading
    if _cached_tz is not None:
        return _cached_tz
    if _loading:
        # Re-entrance during DB read (e.g. log record emitted while opening DB).
        # Return env default without caching so next call retries the DB read.
        from app.config import TIMEZONE

        return _validated_zone(TIMEZONE)

    _loading = True
    try:
        tz_name = _read_from_db()
        if not tz_name:
            from app.config import TIMEZONE

            tz_name = TIMEZONE
        _cached_tz = _validated_zone(tz_name)
    finally:
        _loading = False
    return _cached_tz


def invalidate() -> None:
    """Clear the cached timezone. Must be called after every UI write."""
    global _cached_tz
    _cached_tz = None


def set_timezone(tz: str, db) -> None:
    """Validate, persist and apply a new timezone."""
    if tz != "UTC" and tz not in available_timezones():
        raise ValueError(f"Unknown timezone: {tz!r}")
    from app.services.app_settings_service import _get_or_create

    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    data["timezone"] = tz
    settings.settings_json = data
    db.flush()
    invalidate()


def get_timezone_choices() -> list[str]:
    return CURATED_ZONES
