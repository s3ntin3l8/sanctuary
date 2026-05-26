import json

import pytest

from app.services.intelligence._json import is_effectively_empty, parse_json_response


@pytest.mark.unit
def test_parse_json_response_valid():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }
    raw_text = json.dumps(data)
    result = parse_json_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_json_response_markdown_fence():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }
    raw_text = f"```json\n{json.dumps(data)}\n```"
    result = parse_json_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_json_response_extra_text():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }
    raw_text = (
        f"Here is the result:\n```json\n{json.dumps(data)}\n```\nHope this helps."
    )
    result = parse_json_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_json_response_no_fence_braces():
    data = {
        "legal_significance": "Significance",
        "required_action": "Action",
        "financial_impact": "Impact",
    }
    raw_text = f"Some text before {json.dumps(data)} some text after"
    result = parse_json_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_json_response_invalid():
    raw_text = "not json"
    with pytest.raises(ValueError, match="AI response contains no JSON object"):
        parse_json_response(raw_text)


@pytest.mark.unit
def test_parse_json_response_empty():
    with pytest.raises(ValueError, match="AI returned an empty response"):
        parse_json_response("")


@pytest.mark.unit
def test_parse_json_response_conversational():
    data = {"key": "value"}
    raw_text = (
        "I have analyzed the document. Here is the result in JSON: "
        + json.dumps(data)
        + " I hope this is what you need."
    )
    result = parse_json_response(raw_text)
    assert result == data


@pytest.mark.unit
def test_parse_json_response_truncated():
    raw_text = '{"legal_significance": "something"'
    result = parse_json_response(raw_text)
    assert result == {"legal_significance": "something"}


@pytest.mark.unit
def test_parse_json_response_truncated_mid_string():
    """Regression: LLM hits max_tokens inside a string value — the unterminated
    string must be closed so parse succeeds and prior complete fields are kept."""
    # Simulate a response cut off mid-string-value (the Doc 96 failure pattern)
    raw_text = '{"title": "Ladung", "summary": "Das Gericht hat'
    result = parse_json_response(raw_text)
    assert result["title"] == "Ladung"
    # summary gets the truncated prefix — partial data beats a hard failure
    assert result["summary"].startswith("Das Gericht hat")


@pytest.mark.unit
def test_parse_json_response_truncated_mid_string_nested():
    """Truncation inside a string value inside a nested object."""
    raw_text = '{"outer": {"inner": "partial val'
    result = parse_json_response(raw_text)
    assert result["outer"]["inner"].startswith("partial val")


# ---------------------------------------------------------------------------
# is_effectively_empty: catches the empty-fence pass-2 pathology where qwen3.5
# emits just ```json\n\n``` with the actual JSON living in reasoning_content.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # truly empty inputs
        (None, True),
        ("", True),
        ("   \n\n   ", True),
        # empty markdown fences — the case this helper exists to catch
        ("```json\n\n```", True),
        ("```\n\n```", True),
        ("  ```json\n\n```  ", True),
        ("```json```", True),
        # valid fenced payloads — must NOT be flagged as empty
        ('```json\n{"x": 1}\n```', False),
        ("```json\n  {}\n```", False),  # `{}` is non-empty content
        ('```\n{"a": 2}\n```', False),
        # bare JSON without fences
        ("   {}   ", False),
        ('{"key": "value"}', False),
        # leading prose
        ("Here is the result: {}", False),
    ],
)
@pytest.mark.unit
def test_is_effectively_empty(raw, expected):
    assert is_effectively_empty(raw) is expected
