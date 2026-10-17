"""Stored and logged errors never carry document text (app/core/safe_error.py)."""

import pytest
from pydantic import BaseModel, ValidationError

from app.core.safe_error import describe_exception, sanitize_error_text
from app.services.intelligence._json import parse_json_response

pytestmark = pytest.mark.unit

SENTINEL = "Mustermann-Klage-vertraulich"


class Claim(BaseModel):
    text: str
    count: int


def test_a_json_parse_failure_reports_where_not_what():
    with pytest.raises(ValueError) as excinfo:
        parse_json_response(f'{{"text": "{SENTINEL}" "count": }}')
    assert SENTINEL not in str(excinfo.value)
    assert "position" in str(excinfo.value)


def test_a_response_without_json_does_not_quote_the_answer():
    with pytest.raises(ValueError) as excinfo:
        parse_json_response(f"I cannot help with {SENTINEL}")
    assert SENTINEL not in str(excinfo.value)


def test_a_validation_error_lists_fields_and_types_only():
    with pytest.raises(ValidationError) as excinfo:
        Claim.model_validate({"text": 5, "count": SENTINEL})
    assert SENTINEL in str(excinfo.value)  # pydantic does quote it ...
    described = describe_exception(excinfo.value)
    assert SENTINEL not in described  # ... and we do not
    assert "text (string_type)" in described and "count (int_parsing)" in described


def test_sanitize_drops_quoted_input_collapses_whitespace_and_bounds_length():
    raw = f"1 validation error for X\ntext\n  Input should be x [type=t, input_value='{SENTINEL}', input_type=str]"
    cleaned = sanitize_error_text(raw)
    assert SENTINEL not in cleaned and "\n" not in cleaned
    assert len(sanitize_error_text("x" * 5000)) <= 300


def test_describe_keeps_the_exception_type():
    assert describe_exception(TimeoutError("read timed out")).startswith("TimeoutError")
    assert describe_exception(RuntimeError()) == "RuntimeError"
