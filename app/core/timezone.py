from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Europe/Berlin")


def now() -> datetime:
    """Get current datetime in default timezone (Europe/Berlin)."""
    return datetime.now(DEFAULT_TZ)


def now_utc() -> datetime:
    """Get current UTC datetime."""
    return datetime.now(UTC)


def ensure_tz(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware in default timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=DEFAULT_TZ)
    return dt


def ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_naive(dt: datetime) -> datetime:
    """Convert timezone-aware datetime to naive (strip timezone)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def naive_utc_now() -> datetime:
    """Current UTC time as a tz-naive datetime, matching how DateTime columns are stored."""
    return to_naive(now_utc())


def to_iso(dt: datetime) -> str | None:
    """Convert datetime to ISO string, handling naive datetimes."""
    if dt is None:
        return None
    dt = ensure_tz(dt)
    return dt.isoformat()


def parse_datetime(value: str) -> datetime | None:
    """Parse ISO datetime string, returning timezone-aware in default tz.

    Returns None when `value` is empty or not a valid ISO datetime string.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=DEFAULT_TZ)
        return dt
    except ValueError:
        return None


def format_date(dt: datetime, fmt: str = "%d.%m.%Y") -> str | None:
    """Format datetime as date string."""
    if dt is None:
        return None
    dt = ensure_tz(dt)
    return dt.strftime(fmt)


def format_datetime(dt: datetime, fmt: str = "%d.%m.%Y %H:%M") -> str | None:
    """Format datetime with time."""
    if dt is None:
        return None
    dt = ensure_tz(dt)
    return dt.strftime(fmt)


def compare_dates(dt1: datetime | None, dt2: datetime | None) -> int:
    """Compare two datetimes safely. Returns -1, 0, or 1."""
    dt1 = ensure_tz(dt1) if dt1 else None
    dt2 = ensure_tz(dt2) if dt2 else None

    if dt1 is None and dt2 is None:
        return 0
    if dt1 is None:
        return -1
    if dt2 is None:
        return 1

    if dt1 < dt2:
        return -1
    elif dt1 > dt2:
        return 1
    return 0
