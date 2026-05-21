import logging
from datetime import datetime


class LocalTimeFormatter(logging.Formatter):
    """Renders %(asctime)s in the user-configured timezone (from UserSettings)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        from app.services.timezone_service import get_user_tz

        dt = datetime.fromtimestamp(record.created, tz=get_user_tz())
        return dt.strftime(datefmt) if datefmt else dt.isoformat(timespec="seconds")
