"""Headers-only Gmail access behind the history import (listing, batching, parsing)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

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

    assert [len(b.added) for b in batches] == [50, 50, 21]  # 50-call batches
    assert sleep.call_count == 2  # paced between batches, not after the last
    assert len(found) == 120 and "bad" not in {m["id"] for m in found}
    get = service.users.return_value.messages.return_value.get
    assert get.call_args.kwargs["format"] == "metadata"
    assert get.call_args.kwargs["metadataHeaders"] == [
        "From",
        "Subject",
        "Date",
        "Message-ID",
    ]
