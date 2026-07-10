"""Shared runs.jsonl index writer for every model-call family.

``runs.jsonl`` (under ``DATA_DIR/ai_debug/``) is a machine-readable index of
every AI model call made anywhere in the app — chat/JSON completions, OCR
extraction, embedding generation, and the slicer's page-boundary judgments.
One JSON line per call/run, so jq filters and log greps work uniformly
regardless of which family made the call.

This module owns the writer only. The chat pipeline
(``app.services.intelligence._ai_call``) additionally writes a full-payload
``.md`` debug block per call via ``_write_block`` — that stays chat-only;
non-chat callers get an index row here but no markdown dump.

Living here (not in ``intelligence._ai_call``) because ingestion/embedding
code now needs to call it too, and importing a writer out of the chat
pipeline would point the dependency backwards.
"""

import fcntl
import json
from datetime import datetime

from app.config import DATA_DIR
from app.services.intelligence.prompts import PROMPT_VERSION
from app.services.timezone_service import get_user_tz


def record_run(
    *,
    kind: str,
    scope_id: str,
    stage: str,
    model: str,
    provider: str,
    duration_ms: int,
    status: str,
    doc_id: int | None = None,
    batch_id: int | None = None,
    case_id: str | None = None,
    response_len: int = 0,
    error: str | None = None,
    ttfb_ms: int | None = None,
    thinking_len: int = 0,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    watchdog: str | None = None,
    started_at: str | None = None,
) -> None:
    """Append one entry to runs.jsonl.

    Shared by chat, OCR, embed, and slice callers — chat-only fields
    (``ttfb_ms``, ``thinking_len``, token counts, ``watchdog``) default to
    None/0 for non-chat callers so the schema stays uniform.

    ``doc_id``/``batch_id``/``case_id`` carry the same three-ID
    cross-referencing the chat pipeline uses: pass whichever are known so a
    doc's OCR row joins its chat rows under the same filters.
    """
    entry = {
        "ts": started_at or datetime.now(get_user_tz()).isoformat(timespec="seconds"),
        "prompt_version": PROMPT_VERSION,
        "kind": kind,
        "scope_id": scope_id,
        "stage": stage,
        "doc_id": doc_id,
        "batch_id": batch_id,
        "case_id": case_id,
        "model": model,
        "provider": provider,
        "duration_ms": duration_ms,
        "ttfb_ms": ttfb_ms,
        "response_len": response_len,
        "thinking_len": thinking_len,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "status": status,
        "error": error,
        "watchdog": watchdog,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    # Resolved at call time (not module import time) so DATA_DIR overrides —
    # e.g. tests/conftest.py's isolate_data_dir fixture — take effect.
    index_file = DATA_DIR / "ai_debug" / "runs.jsonl"
    index_file.parent.mkdir(parents=True, exist_ok=True)
    with open(index_file, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
