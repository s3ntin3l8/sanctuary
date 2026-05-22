"""Tests for litellm error-body parsing and transient-backend classification
added in app/services/intelligence/_ai_call.py.

The body shapes here match real failure rows captured from
http://192.168.2.96:4000/spend/logs/v2 today (2026-05-22).
"""

import json

import httpx
import pytest

from app.services.intelligence._ai_call import (
    _parse_litellm_error_code,
    _parse_litellm_error_summary,
    is_transient_backend_error,
)


@pytest.mark.unit
def test_parse_litellm_error_code_extracts_code():
    body = json.dumps({"error": {"message": "x", "code": "500", "type": "T"}}).encode()
    assert _parse_litellm_error_code(body) == "500"


@pytest.mark.unit
def test_parse_litellm_error_code_falls_back_to_type():
    body = json.dumps(
        {"error": {"message": "x", "type": "context_length_exceeded"}}
    ).encode()
    assert _parse_litellm_error_code(body) == "context_length_exceeded"


@pytest.mark.unit
def test_parse_litellm_error_code_handles_garbage():
    assert _parse_litellm_error_code(b"<html>nope</html>") is None
    assert _parse_litellm_error_code(b"") is None


@pytest.mark.unit
def test_parse_litellm_error_summary_real_midstream_body():
    """The exact body shape from the user's reported error at 18:01."""
    body = json.dumps(
        {
            "error": {
                "message": (
                    "litellm.MidStreamFallbackError: litellm.APIConnectionError: "
                    "APIConnectionError: OpenAIException - Model unloaded.. "
                    "Received Model Group=qwen/qwen3.5-9b"
                ),
                "type": "MidStreamFallbackError",
                "code": "500",
                "param": None,
            }
        }
    ).encode()
    out = _parse_litellm_error_summary(body)
    assert out is not None
    assert "MidStreamFallbackError" in out
    assert "Model unloaded" in out


@pytest.mark.unit
def test_parse_litellm_error_summary_real_load_failure():
    body = json.dumps(
        {
            "error": {
                "message": 'Lm_studioException - Failed to load model "qwen/qwen3.5-9b"',
                "type": "BadRequestError",
                "code": "400",
            }
        }
    ).encode()
    out = _parse_litellm_error_summary(body)
    assert out is not None
    assert "BadRequestError" in out
    assert "Failed to load model" in out


@pytest.mark.unit
def test_parse_litellm_error_summary_handles_garbage():
    assert _parse_litellm_error_summary(b"<html>nope</html>") is None
    assert _parse_litellm_error_summary(b"") is None
    assert _parse_litellm_error_summary(b'{"not": "error"}') is None


# ---------------------------------------------------------------------------
# is_transient_backend_error — markers from today's litellm catalog
# ---------------------------------------------------------------------------


def _make_status_error(msg: str) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError whose str() contains the message shape we'd
    splice into stream_error via _ai_call._stream_response."""
    request = httpx.Request("POST", "http://x/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return httpx.HTTPStatusError(msg, request=request, response=response)


@pytest.mark.unit
@pytest.mark.parametrize(
    "msg",
    [
        "HTTP 400 [400] BadRequestError: Lm_studioException - Failed to load model qwen/qwen3.5-9b",
        "HTTP 400 [400] BadRequestError: Lm_studioException - Model has not started loading",
        "HTTP 500 [500] MidStreamFallbackError: Model unloaded mid-stream",
        "HTTP 500 [500] api_connection_error: Lm_studioException - upstream",
    ],
)
def test_is_transient_backend_error_recognizes_markers(msg):
    assert is_transient_backend_error(_make_status_error(msg)) is True


@pytest.mark.unit
def test_is_transient_backend_error_rejects_genuine_client_errors():
    # context_length_exceeded is a genuine client-side condition (prompt too
    # long) — same prompt always fails. Must NOT be classified transient.
    msg = "HTTP 400 [context_length_exceeded] Prompt too long for this model"
    assert is_transient_backend_error(_make_status_error(msg)) is False

    # Bare HTTP error with no body summary — no markers, treat as client-side.
    assert is_transient_backend_error(_make_status_error("HTTP 400 [400]")) is False
