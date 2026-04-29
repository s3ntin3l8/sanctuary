"""Pin: parse_json_response repairs nested truncated AI responses.

Every intelligence prompt produces nested JSON (key_passages: list of objects,
management_summary: object, etc.). When the LLM truncates mid-stream, the old
repair appended a single `}` — leaving e.g. `{"a":{"b":1` → `{"a":{"b":1}`,
still invalid. This test pins multi-level repair.
"""

import pytest

from app.services.intelligence._json import parse_json_response


@pytest.mark.unit
def test_repair_nested_object_one_level_truncated():
    """`{"a":{"b":1` → repaired to `{"a":{"b":1}}` and parsed."""
    result = parse_json_response('{"a":{"b":1')
    assert result == {"a": {"b": 1}}


@pytest.mark.unit
def test_repair_nested_with_array():
    """`{"items":[1,2,3` → repaired to `{"items":[1,2,3]}`."""
    result = parse_json_response('{"items":[1,2,3')
    assert result == {"items": [1, 2, 3]}


@pytest.mark.unit
def test_repair_mixed_braces_and_brackets():
    """Object containing array of objects, all truncated."""
    result = parse_json_response('{"a":[{"b":2},{"c":3')
    assert result == {"a": [{"b": 2}, {"c": 3}]}


@pytest.mark.unit
def test_repair_does_not_count_braces_inside_strings():
    """String literal containing `{` or `[` must not affect the bracket count."""
    result = parse_json_response('{"msg":"hello {world}","n":[1,2')
    assert result == {"msg": "hello {world}", "n": [1, 2]}


@pytest.mark.unit
def test_repair_handles_escaped_quotes():
    """Backslash-escaped quotes inside strings shouldn't end the string."""
    result = parse_json_response('{"k":"a\\"b","arr":[1')
    assert result == {"k": 'a"b', "arr": [1]}


@pytest.mark.unit
def test_complete_object_passes_through():
    """Already-valid JSON should not be modified."""
    result = parse_json_response('{"a":1, "b": [2, 3]}')
    assert result == {"a": 1, "b": [2, 3]}


@pytest.mark.unit
def test_no_json_at_all_raises():
    """If there's no `{` at all, raise ValueError."""
    with pytest.raises(ValueError, match="no JSON object"):
        parse_json_response("plain text response")
