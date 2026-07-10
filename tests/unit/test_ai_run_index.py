"""Tests for the shared runs.jsonl writer (app.services.ai_run_index).

record_run() is the single append point shared by the chat pipeline
(_ai_call.py), OCR (ingestion/service.py), embeddings (embeddings.py), and
the slicer (ingestion/slicer.py) — see app/services/ai_run_index.py.
"""

import json

import pytest


def _read_runs_jsonl(data_dir):
    path = data_dir / "ai_debug" / "runs.jsonl"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.mark.unit
def test_record_run_writes_valid_json_line(isolate_data_dir):
    from app.services.ai_run_index import record_run

    record_run(
        kind="doc",
        scope_id="123",
        stage="ocr",
        doc_id=123,
        batch_id=7,
        case_id="ADV-024-A",
        model="chandra-vision",
        provider="chandra",
        duration_ms=4200,
        status="ok",
        response_len=1500,
    )

    rows = [
        r
        for r in _read_runs_jsonl(isolate_data_dir)
        if r.get("stage") == "ocr" and r.get("doc_id") == 123
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "doc"
    assert row["scope_id"] == "123"
    assert row["doc_id"] == 123
    assert row["batch_id"] == 7
    assert row["case_id"] == "ADV-024-A"
    assert row["model"] == "chandra-vision"
    assert row["provider"] == "chandra"
    assert row["duration_ms"] == 4200
    assert row["status"] == "ok"
    assert row["response_len"] == 1500
    assert "ts" in row
    assert "prompt_version" in row


@pytest.mark.unit
def test_record_run_defaults_chat_only_fields_for_non_chat_callers(isolate_data_dir):
    """OCR/embed/slice callers don't pass ttfb/tokens/thinking/watchdog —
    the schema stays uniform with those fields present but null/zero."""
    from app.services.ai_run_index import record_run

    record_run(
        kind="doc",
        scope_id="456",
        stage="embed",
        doc_id=456,
        model="nomic-embed-text",
        provider="ollama",
        duration_ms=300,
        status="ok",
        response_len=768,
    )

    rows = [
        r
        for r in _read_runs_jsonl(isolate_data_dir)
        if r.get("stage") == "embed" and r.get("doc_id") == 456
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["ttfb_ms"] is None
    assert row["thinking_len"] == 0
    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None
    assert row["reasoning_tokens"] is None
    assert row["watchdog"] is None


@pytest.mark.unit
def test_record_run_records_error_status_with_message(isolate_data_dir):
    from app.services.ai_run_index import record_run

    record_run(
        kind="doc",
        scope_id="789",
        stage="ocr",
        doc_id=789,
        model="chandra-vision",
        provider="chandra",
        duration_ms=0,
        status="error",
        error="all 3 pages failed OCR",
    )

    rows = [
        r
        for r in _read_runs_jsonl(isolate_data_dir)
        if r.get("stage") == "ocr" and r.get("doc_id") == 789
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "all 3 pages failed OCR"


@pytest.mark.unit
def test_record_run_appends_rather_than_overwrites(isolate_data_dir):
    from app.services.ai_run_index import record_run

    for i in range(3):
        record_run(
            kind="batch",
            scope_id="99",
            stage="slice",
            batch_id=99,
            model="qwen3.5:9b",
            provider="ollama",
            duration_ms=100 * i,
            status="ok",
            response_len=i,
        )

    rows = [
        r
        for r in _read_runs_jsonl(isolate_data_dir)
        if r.get("stage") == "slice" and r.get("batch_id") == 99
    ]
    assert len(rows) == 3
