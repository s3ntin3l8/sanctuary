"""Gmail request handling: rate limits are waited out, a dead grant ends the run."""

import json
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app.services.ingestion import gmail
from app.services.ingestion.gmail import (
    GmailMessageGone,
    GmailMessageTooLarge,
    GmailReconnectRequired,
    execute_with_retry,
    fetch_raw_message,
)

pytestmark = pytest.mark.unit


def _http_error(status: int, reason: str = "other"):
    body = {
        "error": {
            "code": status,
            "message": "x",
            "errors": [{"message": "x", "domain": "global", "reason": reason}],
        }
    }
    return HttpError(
        httplib2.Response({"status": str(status)}),
        json.dumps(body).encode(),
        uri="https://gmail.googleapis.com/x",
    )


def _request(*outcomes):
    request = MagicMock()
    request.execute.side_effect = list(outcomes)
    return request


def test_a_rate_limited_request_is_retried_until_it_works():
    request = _request(
        _http_error(429), _http_error(403, "rateLimitExceeded"), {"ok": 1}
    )
    with patch.object(gmail.time, "sleep") as sleep:
        assert execute_with_retry(request) == {"ok": 1}
    assert sleep.call_count == 2


def test_rate_limits_that_never_clear_are_raised():
    request = _request(*[_http_error(429)] * 10)
    with patch.object(gmail.time, "sleep"), pytest.raises(HttpError):
        execute_with_retry(request)
    assert request.execute.call_count == 5  # first try + one per wait


def test_other_errors_are_not_retried():
    request = _request(_http_error(403, "insufficientPermissions"), {"ok": 1})
    with patch.object(gmail.time, "sleep") as sleep, pytest.raises(HttpError):
        execute_with_retry(request)
    sleep.assert_not_called()


@pytest.mark.parametrize("failure", [RefreshError("revoked"), _http_error(401)])
def test_a_dead_grant_asks_for_a_reconnect(failure):
    with pytest.raises(GmailReconnectRequired):
        execute_with_retry(_request(failure))


def test_a_deleted_message_is_reported_gone():
    service = MagicMock()
    get = service.users.return_value.messages.return_value.get
    get.return_value.execute.side_effect = _http_error(404)
    with pytest.raises(GmailMessageGone):
        fetch_raw_message(service, "g1")


def _raw_response(raw: bytes) -> dict:
    import base64

    return {
        "id": "g1",
        "threadId": "t1",
        "internalDate": "1780000000000",
        "raw": base64.urlsafe_b64encode(raw).decode(),
    }


def test_an_oversized_message_is_refused_before_it_is_decoded(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "MAX_EMAIL_SIZE_MB", 1)
    service = MagicMock()
    get = service.users.return_value.messages.return_value.get
    get.return_value.execute.return_value = _raw_response(b"x" * (2 * 1024 * 1024))
    with patch("base64.urlsafe_b64decode") as decode:
        with pytest.raises(GmailMessageTooLarge):
            fetch_raw_message(service, "g1")
    decode.assert_not_called()


def test_a_message_within_the_limit_still_reports_its_attachments():
    raw = (
        b"From: a@example.com\r\nSubject: s\r\nMessage-ID: <m@x>\r\n"
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="a.pdf"\r\n\r\n%PDF\r\n--B--\r\n'
    )
    service = MagicMock()
    get = service.users.return_value.messages.return_value.get
    get.return_value.execute.return_value = _raw_response(raw)
    result = fetch_raw_message(service, "g1")
    assert result == raw
    assert result.metadata["has_attachments"] is True
