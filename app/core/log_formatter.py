import logging
import traceback
from datetime import datetime
from types import TracebackType

from app.core.safe_error import describe_exception


class LocalTimeFormatter(logging.Formatter):
    """Renders %(asctime)s in the user-configured timezone (from UserSettings)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        from app.services.timezone_service import get_user_tz

        dt = datetime.fromtimestamp(record.created, tz=get_user_tz())
        return dt.strftime(datefmt) if datefmt else dt.isoformat(timespec="seconds")

    def formatException(
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        """The traceback with its frames, but each exception's message replaced
        by ``describe_exception``: a parse or validation error quotes the model
        answer (document text), and the stock traceback would print it again."""
        exc = ei[1]
        if exc is None:
            return super().formatException(ei)
        return "".join(_chain(exc, set())).rstrip("\n")


_CAUSE = "\nThe above exception was the direct cause of the following exception:\n\n"
_CONTEXT = "\nDuring handling of the above exception, another exception occurred:\n\n"


def _chain(exc: BaseException, seen: set[int]) -> list[str]:
    seen.add(id(exc))
    lines: list[str] = []
    if exc.__cause__ is not None and id(exc.__cause__) not in seen:
        lines += _chain(exc.__cause__, seen) + [_CAUSE]
    elif (
        exc.__context__ is not None
        and not exc.__suppress_context__
        and id(exc.__context__) not in seen
    ):
        lines += _chain(exc.__context__, seen) + [_CONTEXT]
    lines.append("Traceback (most recent call last):\n")
    lines += traceback.format_tb(exc.__traceback__)
    lines.append(describe_exception(exc) + "\n")
    return lines
