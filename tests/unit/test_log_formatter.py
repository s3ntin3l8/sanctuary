"""Tracebacks keep their frames but never print an exception's message."""

import logging
import sys

import pytest
from pydantic import BaseModel, ValidationError

from app.core.log_formatter import LocalTimeFormatter

pytestmark = pytest.mark.unit

SENTINEL = "Klage gegen Mustermann wegen Mietrückstand"


class _Answer(BaseModel):
    count: int


def _record() -> logging.LogRecord:
    try:
        try:
            _Answer.model_validate({"count": SENTINEL})
        except ValidationError as inner:
            raise RuntimeError("model answer unusable") from inner
    except RuntimeError:
        return logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "task failed", None, sys.exc_info()
        )


def test_the_traceback_keeps_frames_and_drops_quoted_model_input():
    text = LocalTimeFormatter("%(message)s").format(_record())
    assert SENTINEL not in text
    assert "task failed" in text
    assert "test_log_formatter.py" in text and "_record" in text
    assert "ValidationError" in text and "RuntimeError" in text
    assert "direct cause" in text


def test_a_long_message_is_bounded():
    try:
        raise RuntimeError("x" * 5000)
    except RuntimeError:
        rec = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "m", None, sys.exc_info()
        )
    assert len(LocalTimeFormatter("%(message)s").format(rec)) < 1500


def test_a_record_without_an_exception_is_untouched():
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "plain %s", ("x",), None)
    assert LocalTimeFormatter("%(message)s").format(rec) == "plain x"


def test_celery_task_failure_line_carries_no_quoted_model_input():
    from app.tasks.celery_app import _SanitizeTaskFailure

    try:
        _Answer.model_validate({"count": SENTINEL})
    except ValidationError as exc:
        exc_info = sys.exc_info()
        raw = repr(exc)
    assert SENTINEL in raw
    rec = logging.LogRecord(
        "celery.app.trace",
        logging.ERROR,
        __file__,
        1,
        "Task %(name)s[%(id)s] raised unexpected: %(exc)s",
        ({"name": "t", "id": "1", "exc": raw},),  # a lone dict becomes args
        exc_info,
    )
    assert _SanitizeTaskFailure().filter(rec)
    assert SENTINEL not in rec.getMessage()
    assert "ValidationError" in rec.getMessage()


def _validation_error() -> ValidationError:
    try:
        _Answer.model_validate({"count": SENTINEL})
    except ValidationError as exc:
        return exc
    raise AssertionError


def test_celery_retry_line_without_exc_info_is_sanitised():
    from app.tasks.celery_app import _SanitizeTaskFailure

    raw = repr(_validation_error())
    assert SENTINEL in raw
    rec = logging.LogRecord(
        "celery.app.trace",
        logging.INFO,
        __file__,
        1,
        "Task %(name)s retry: %(exc)s",
        ({"name": "t", "exc": raw},),
        None,
    )
    _SanitizeTaskFailure().filter(rec)
    assert SENTINEL not in rec.getMessage()


def test_celery_worker_request_line_is_sanitised():
    from app.tasks.celery_app import _SanitizeTaskFailure

    rec = logging.LogRecord(
        "celery.worker.request",
        logging.ERROR,
        __file__,
        1,
        "Task handler raised error: %r",
        (_validation_error(),),
        None,
    )
    _SanitizeTaskFailure().filter(rec)
    assert SENTINEL not in rec.getMessage()
    assert "ValidationError" in rec.getMessage()
