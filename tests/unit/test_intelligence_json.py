import json

import pytest

from app.services.intelligence._json import parse_json_response


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
