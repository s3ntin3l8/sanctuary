"""Headers-only Gmail access behind the history import (listing, batching, parsing)."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.services.ingestion import gmail
from app.services.ingestion.gmail import (
    fetch_metadata,
    list_message_ids,
    parse_metadata,
)

pytestmark = pytest.mark.unit


def _message(**overrides):
    base = {
        "id": "g1",
        "threadId": "t1",
        "internalDate": str(int(datetime(2024, 5, 2, tzinfo=UTC).timestamp() * 1000)),
        "sizeEstimate": 1234,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Dr. Vogt <Lawyer@Example.COM>"},
                {"name": "Subject", "value": "8372/25 Schriftsatz"},
                {"name": "Date", "value": "Mon, 06 May 2024 10:30:00 +0200"},
                {"name": "Message-ID", "value": " <abc@mail.example.com> "},
            ],
        },
    }
    base.update(overrides)
    return base


def test_parse_metadata_normalises_headers():
    meta = parse_metadata(_message())
    assert meta["gmail_id"] == "g1" and meta["thread_id"] == "t1"
    assert meta["sender"] == "lawyer@example.com"
    assert meta["subject"] == "8372/25 Schriftsatz"
    assert meta["message_id"] == "<abc@mail.example.com>"
    # Sent time comes from the Date header, with its offset.
    assert meta["sent_at"] == datetime(2024, 5, 6, 8, 30, tzinfo=UTC)
    assert meta["size_estimate"] == 1234
    assert meta["has_attachments"] is False


def test_parse_metadata_decodes_encoded_subjects():
    message = _message()
    message["payload"]["headers"][1]["value"] = (
        "=?UTF-8?Q?8372/25_Fr=C3=BChst=C3=BCck?="
    )
    assert parse_metadata(message)["subject"] == "8372/25 Frühstück"


def test_parse_metadata_falls_back_to_gmails_receipt_time():
    message = _message()
    message["payload"]["headers"][2]["value"] = "not a date"
    assert parse_metadata(message)["sent_at"] == datetime(2024, 5, 2, tzinfo=UTC)


def test_parse_metadata_missing_message_id_is_none():
    message = _message()
    message["payload"]["headers"] = [
        h for h in message["payload"]["headers"] if h["name"] != "Message-ID"
    ]
    assert parse_metadata(message)["message_id"] is None


def test_parse_metadata_attachment_detection():
    with_part = _message()
    with_part["payload"]["parts"] = [
        {"filename": "", "mimeType": "text/plain"},
        {"filename": "a.pdf"},
    ]
    assert parse_metadata(with_part)["has_attachments"] is True

    mixed = _message()
    mixed["payload"]["mimeType"] = "multipart/mixed"  # metadata format may omit parts
    assert parse_metadata(mixed)["has_attachments"] is True


def test_list_message_ids_pages_through_results():
    pages = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "next"},
        {"messages": [{"id": "c"}]},
    ]
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = pages
    with patch.object(gmail.time, "sleep"):
        assert list(list_message_ids(service, "q")) == ["a", "b", "c"]
    calls = service.users.return_value.messages.return_value.list.call_args_list
    assert calls[1].kwargs["pageToken"] == "next"
    assert "pageToken" not in calls[0].kwargs


class _FakeBatch:
    def __init__(self, callback, fail_ids):
        self.callback, self.fail_ids, self.added = callback, fail_ids, []

    def add(self, request, request_id):
        self.added.append((request, request_id))

    def execute(self):
        for request, request_id in self.added:
            if request_id in self.fail_ids:
                self.callback(request_id, None, RuntimeError("quota"))
            else:
                self.callback(request_id, {"id": request_id}, None)


def test_fetch_metadata_batches_requests_and_skips_failures():
    batches = []

    def _new_batch(callback):
        batch = _FakeBatch(callback, fail_ids={"bad"})
        batches.append(batch)
        return batch

    service = MagicMock()
    service.new_batch_http_request.side_effect = _new_batch
    ids = [f"m{i}" for i in range(120)] + ["bad"]

    with patch.object(gmail.time, "sleep") as sleep:
        found = fetch_metadata(service, ids)

    assert [len(b.added) for b in batches] == [25, 25, 25, 25, 21]
    assert sleep.call_count == 4  # paced between batches, not after the last
    assert len(found) == 120 and "bad" not in {m["id"] for m in found}
    get = service.users.return_value.messages.return_value.get
    assert get.call_args.kwargs["format"] == "metadata"
    assert get.call_args.kwargs["metadataHeaders"] == [
        "From",
        "To",
        "Subject",
        "Date",
        "Message-ID",
    ]


# --- rate limits: retried, not dropped --------------------------------------


def _http_error(status: int, reason: str, message: str = "x"):
    body = {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"message": message, "domain": "global", "reason": reason}],
        }
    }
    return HttpError(
        httplib2.Response({"status": str(status)}),
        json.dumps(body).encode(),
        uri="https://gmail.googleapis.com/x",
    )


class _FlakyBatch:
    """Fails each id with its scripted errors (consumed one per attempt), then succeeds."""

    def __init__(self, callback, script):
        self.callback, self.script, self.added = callback, script, []

    def add(self, request, request_id):
        self.added.append(request_id)

    def execute(self):
        for request_id in self.added:
            errors = self.script.get(request_id)
            if errors:
                self.callback(request_id, None, errors.pop(0))
            else:
                self.callback(request_id, {"id": request_id}, None)


def _service(script, sizes=None):
    service = MagicMock()

    def _new_batch(callback):
        batch = _FlakyBatch(callback, script)
        if sizes is not None:
            sizes.append(batch)
        return batch

    service.new_batch_http_request.side_effect = _new_batch
    return service


def test_rate_limited_requests_are_retried_until_they_succeed():
    quota = _http_error(403, "rateLimitExceeded", "Quota exceeded for quota metric")
    busy = _http_error(429, "rateLimitExceeded", "Too many concurrent requests")
    script = {"a": [quota, busy], "b": [busy]}
    service = _service(script)

    with patch.object(gmail.time, "sleep") as sleep:
        found = fetch_metadata(service, ["a", "b", "c"])

    assert sorted(m["id"] for m in found) == ["a", "b", "c"]
    waits = [c.args[0] for c in sleep.call_args_list if c.args[0] >= 5]
    assert waits == [5, 15]  # grows between retry rounds


def test_retries_use_smaller_batches_and_stop_after_the_last_round():
    always = {"a": [_http_error(429, "rateLimitExceeded")] * 50}
    batches = []
    service = _service(always, batches)

    with patch.object(gmail.time, "sleep"):
        found = fetch_metadata(service, ["a"] + [f"m{i}" for i in range(30)])

    assert "a" not in {m["id"] for m in found} and len(found) == 30
    assert len(batches) == 2 + gmail._METADATA_RETRY_ROUNDS  # 31 ids → 2, then 1/round


def test_deleted_messages_are_reported_gone_not_failed(caplog):
    script = {"x": [_http_error(404, "notFound", "Requested entity was not found.")]}
    gone: set[str] = set()
    with patch.object(gmail.time, "sleep") as sleep:
        found = fetch_metadata(_service(script), ["x", "y"], gone=gone)
    assert [m["id"] for m in found] == ["y"] and gone == {"x"}
    assert not any(c.args[0] >= 5 for c in sleep.call_args_list)  # not retried
    assert "fetch failed" not in caplog.text


def test_other_403_and_unknown_errors_are_final_not_retried():
    script = {
        "a": [_http_error(403, "insufficientPermissions", "denied")],
        "b": [RuntimeError("boom")],
    }
    batches = []
    with patch.object(gmail.time, "sleep"):
        found = fetch_metadata(_service(script, batches), ["a", "b", "c"])
    assert [m["id"] for m in found] == ["c"]
    assert len(batches) == 1


def test_a_throttled_batch_slows_the_pace_for_the_rest():
    busy = _http_error(429, "rateLimitExceeded")
    script = {"m0": [busy]}
    ids = [f"m{i}" for i in range(60)]
    with patch.object(gmail.time, "sleep") as sleep:
        fetch_metadata(_service(script), ids)
    pauses = [c.args[0] for c in sleep.call_args_list]
    assert pauses[0] == gmail._BATCH_PAUSE_SECONDS * 2  # first batch tripped it


def test_index_chunk_does_not_count_deleted_messages_as_skipped():
    from app.tasks import gmail_sync

    def _fetch(service, ids, gone):
        gone.add("deleted")
        return [{"id": "ok"}]

    with (
        patch.object(gmail_sync, "fetch_metadata", side_effect=_fetch),
        patch.object(gmail_sync, "parse_metadata", side_effect=lambda raw: raw),
        patch.object(gmail_sync.gmail_index_service, "upsert_metadata"),
    ):
        skipped = gmail_sync._index_chunk(
            MagicMock(), MagicMock(), 1, ["ok", "deleted", "throttled"]
        )
    assert skipped == 1  # only the one Gmail kept failing


def test_a_rate_limited_batch_call_is_retried_like_its_sub_requests():
    envelope = _http_error(429, "rateLimitExceeded", "Too many concurrent requests")
    refused = []

    def _new_batch(callback):
        batch = _FlakyBatch(callback, {})
        if not refused:
            refused.append(batch)

            def _boom():
                raise envelope

            batch.execute = _boom
        return batch

    service = MagicMock()
    service.new_batch_http_request.side_effect = _new_batch

    with patch.object(gmail.time, "sleep"):
        found = fetch_metadata(service, ["a", "b", "c"])

    assert sorted(m["id"] for m in found) == ["a", "b", "c"]


def test_a_non_rate_limit_batch_failure_still_raises():
    denied = _http_error(403, "insufficientPermissions", "denied")
    service = MagicMock()
    batch = MagicMock()
    batch.execute.side_effect = denied
    service.new_batch_http_request.return_value = batch
    with patch.object(gmail.time, "sleep"), pytest.raises(HttpError):
        fetch_metadata(service, ["a"])


def test_a_403_without_an_error_list_is_final_and_does_not_crash():
    for body in (b'{"error": {"code": 403, "message": "nope"}}', b"<html>no</html>"):
        error = HttpError(httplib2.Response({"status": "403"}), body, uri="https://x")
        assert gmail._is_rate_limited(error) is False
    script = {"a": [HttpError(httplib2.Response({"status": "403"}), b"<html/>")]}
    with patch.object(gmail.time, "sleep"):
        found = fetch_metadata(_service(script), ["a", "b"])
    assert [m["id"] for m in found] == ["b"]


def test_gives_up_with_a_warning_and_deleted_messages_log_one_summary(caplog):
    import logging

    caplog.set_level(logging.INFO, logger=gmail.logger.name)
    script = {
        "a": [_http_error(429, "rateLimitExceeded")] * 50,
        **{f"d{i}": [_http_error(404, "notFound")] for i in range(5)},
    }
    with patch.object(gmail.time, "sleep"):
        fetch_metadata(_service(script), ["a"] + [f"d{i}" for i in range(5)])
    assert "gave up on 1 rate-limited" in caplog.text
    assert caplog.text.count("no longer exist") == 1
    assert "5 Gmail message(s) no longer exist" in caplog.text
