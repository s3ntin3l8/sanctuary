"""Error text that is safe to store and log: no document or email content.

Exception messages are built from whatever failed, and in this app that is often
a document: a JSON parse error quotes the model's answer, a pydantic
``ValidationError`` quotes the offending ``input_value``, a conversion error
echoes page text, a database error carries bound parameters. Stage errors, run
indexes and briefing rows persist those strings and the Settings pages show
them, so they go through here first.

``describe_exception`` builds a message from the exception's *structure* (type,
and for validation errors the failing field paths and error types, never the
input). ``sanitize_error_text`` is the backstop for a string we did not build:
one line, bounded, with pydantic's quoted input removed.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

STORED_ERROR_LIMIT = 300

_INPUT_VALUE = re.compile(r"input_value=.*?(?=, input_type=|\]\s*$|\n|$)", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


def sanitize_error_text(text: str, limit: int = STORED_ERROR_LIMIT) -> str:
    """One bounded line; pydantic's quoted ``input_value=...`` is dropped."""
    text = _INPUT_VALUE.sub("input_value=<omitted>", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_exception(exc: BaseException, limit: int = STORED_ERROR_LIMIT) -> str:
    """``Type: message`` where the message is built from structure when the
    exception is a validation error, and sanitised text otherwise."""
    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'} ({err['type']})"
            for err in exc.errors(
                include_input=False, include_url=False, include_context=False
            )[:5]
        )
        return sanitize_error_text(
            f"{type(exc).__name__}: {exc.error_count()} validation error(s): {problems}",
            limit,
        )
    message = sanitize_error_text(str(exc), limit)
    name = type(exc).__name__
    return f"{name}: {message}" if message else name
