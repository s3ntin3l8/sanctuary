"""Shared synchronous AI streaming helper for all intelligence stages."""

import fcntl
import json
import logging
import re
import time
import traceback
from datetime import UTC, datetime

import httpx

from app.config import AI_READ_TIMEOUT, DATA_DIR
from app.core.async_utils import run_async
from app.services.ai_config import get_chat_config
from app.services.ai_provider import chat_provider
from app.services.intelligence._json import parse_json_response

logger = logging.getLogger(__name__)

_SEPARATOR = "═" * 64
_SECTION = "──"
_LABEL_RE = re.compile(r"^(doc|batch|case)_(.+)_([^_]+)$")


def _scope_file(debug_dir, debug_label: str):
    """Derive the per-scope log file path from a debug_label."""
    m = _LABEL_RE.match(debug_label)
    if m:
        kind, scope_id, _ = m.groups()
        return debug_dir / f"{kind}_{scope_id}.log"
    return debug_dir / f"misc_{debug_label}.log"


def _write_block(
    scope_file,
    *,
    debug_label: str,
    stage: str,
    started_at: str,
    model: str,
    provider: str,
    kind: str,
    scope_id: str,
    ingest_batch_id,
    duration_ms: int,
    ttfb_ms: int | None,
    payload: dict,
    thinking: str,
    response: str,
    error: str | None,
) -> None:
    status = "error" if error else "ok"
    doc_id = int(scope_id) if kind == "doc" else None
    batch_id = int(scope_id) if kind == "batch" else None
    case_id = scope_id if kind == "case" else None

    header = (
        f"{_SEPARATOR}\n"
        f"call: {debug_label} | stage={stage} | ts={started_at}\n"
        f"model={model} | provider={provider}\n"
    )
    if doc_id is not None:
        header += f"doc_id={doc_id} | ingest_batch_id={ingest_batch_id}\n"
    elif batch_id is not None:
        header += f"batch_id={batch_id} | ingest_batch_id={ingest_batch_id}\n"
    elif case_id is not None:
        header += f"case_id={case_id}\n"

    ttfb_str = f"{ttfb_ms}" if ttfb_ms is not None else "n/a"
    header += (
        f"duration_ms={duration_ms} | ttfb_ms={ttfb_str}\n"
        f"response_len={len(response)} | thinking_len={len(thinking)} | status={status}\n"
    )

    body = (
        f"{_SECTION} payload {_SECTION}\n"
        f"{json.dumps(payload)}\n"
        f"{_SECTION} thinking {_SECTION}\n"
        f"{thinking}\n"
    )
    if error:
        body += f"{_SECTION} error {_SECTION}\n{error}\n"
    else:
        body += f"{_SECTION} response {_SECTION}\n{response}\n"
    body += f"{_SECTION} end {_SECTION}\n"

    block = header + body

    with open(scope_file, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(block)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _append_index(
    index_file,
    *,
    started_at: str,
    kind: str,
    scope_id: str,
    stage: str,
    ingest_batch_id,
    model: str,
    provider: str,
    duration_ms: int,
    ttfb_ms: int | None,
    response_len: int,
    thinking_len: int,
    status: str,
    error: str | None,
) -> None:
    doc_id = int(scope_id) if kind == "doc" else None
    batch_id = int(scope_id) if kind == "batch" else None
    case_id = scope_id if kind == "case" else None

    entry = {
        "ts": started_at,
        "kind": kind,
        "scope_id": scope_id,
        "stage": stage,
        "doc_id": doc_id,
        "batch_id": batch_id,
        "case_id": case_id,
        "ingest_batch_id": ingest_batch_id,
        "model": model,
        "provider": provider,
        "duration_ms": duration_ms,
        "ttfb_ms": ttfb_ms,
        "response_len": response_len,
        "thinking_len": thinking_len,
        "status": status,
        "error": error,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    with open(index_file, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def call_json_ai(
    *,
    system_prompt: str,
    user_prompt: str,
    options: dict,
    debug_label: str,
    model: str | None = None,
    db=None,
    ingest_batch_id: int | None = None,
    suppress_thinking: bool = False,
) -> dict:
    """Synchronous streaming AI call that returns a parsed JSON dict.

    Args:
        system_prompt: The system prompt to send.
        user_prompt: The user/document prompt to send.
        options: Provider options (num_ctx, temperature, num_predict, max_tokens, …).
        debug_label: Short label used in the debug log filename, e.g. "doc_42_entities".
        model: Override the configured summary model. If None, uses cfg.summary_model.
        db: SQLAlchemy session — if provided, reloads ai_provider config from DB first.
        ingest_batch_id: IngestBatch.id for cross-stage traceability in the index.

    Returns:
        Parsed dict from the AI JSON response.

    Raises:
        ValueError: If the AI returns an empty response, or if JSON parsing fails.
        httpx.HTTPStatusError: On non-2xx HTTP responses.
    """
    if db is not None:
        chat_provider.reload_from_db(db)

    cfg = get_chat_config(db)
    resolved_model = model or cfg.summary_model

    if suppress_thinking:
        system_prompt = (
            system_prompt + "\n\nIMPORTANT: Respond with the final JSON only. "
            "Do not include reasoning, explanation, or <think> blocks."
        )
        user_prompt = "/no_think\n" + user_prompt

    params = run_async(
        chat_provider.get_generate_params(
            model=resolved_model,
            prompt=user_prompt,
            system_prompt=system_prompt,
            stream=True,
            options=options,
        )
    )
    ptype = run_async(chat_provider.get_type())

    debug_dir = DATA_DIR / "ai_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    scope_file = _scope_file(debug_dir, debug_label)
    index_file = debug_dir / "runs.jsonl"

    m = _LABEL_RE.match(debug_label)
    if m:
        kind, scope_id, stage = m.groups()
    else:
        kind, scope_id, stage = "misc", debug_label, debug_label

    started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    start_perf = time.perf_counter()
    ttfb_perf: float | None = None

    full_thinking = ""
    full_response = ""
    stream_error: str | None = None

    try:
        with httpx.Client(
            timeout=httpx.Timeout(
                connect=5.0, read=AI_READ_TIMEOUT, write=30.0, pool=10.0
            )
        ) as client:
            with client.stream(
                "POST", params["url"], json=params["json"], headers=params["headers"]
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    chunk = chat_provider.parse_stream_line(line, ptype)
                    if not chunk:
                        continue

                    if "thinking" in chunk:
                        full_thinking += chunk["thinking"]
                    if "response" in chunk:
                        token = chunk["response"]
                        if token:
                            if ttfb_perf is None:
                                ttfb_perf = time.perf_counter()
                            full_response += token
                    if chunk.get("done"):
                        break
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        # Fast-fail: host is unreachable — log compactly and re-raise immediately.
        duration_so_far = int((time.perf_counter() - start_perf) * 1000)
        stream_error = (
            f"connection failed after {duration_so_far}ms: {type(e).__name__}: {e}"
        )
        raise
    except (httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
        # Transient: server is up but slow — callers may retry.
        duration_so_far = int((time.perf_counter() - start_perf) * 1000)
        ttfb_so_far = (
            int((ttfb_perf - start_perf) * 1000) if ttfb_perf is not None else None
        )
        stream_error = (
            f"timeout after {duration_so_far}ms"
            + (f", ttfb={ttfb_so_far}ms" if ttfb_so_far is not None else "")
            + f": {type(e).__name__}"
        )
        raise
    except Exception as e:
        stream_error = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        raise
    finally:
        duration_ms = int((time.perf_counter() - start_perf) * 1000)
        ttfb_ms = (
            int((ttfb_perf - start_perf) * 1000) if ttfb_perf is not None else None
        )

        _write_block(
            scope_file,
            debug_label=debug_label,
            stage=stage,
            started_at=started_at,
            model=resolved_model,
            provider=ptype,
            kind=kind,
            scope_id=scope_id,
            ingest_batch_id=ingest_batch_id,
            duration_ms=duration_ms,
            ttfb_ms=ttfb_ms,
            payload=params["json"],
            thinking=full_thinking,
            response=full_response,
            error=stream_error,
        )
        _append_index(
            index_file,
            started_at=started_at,
            kind=kind,
            scope_id=scope_id,
            stage=stage,
            ingest_batch_id=ingest_batch_id,
            model=resolved_model,
            provider=ptype,
            duration_ms=duration_ms,
            ttfb_ms=ttfb_ms,
            response_len=len(full_response),
            thinking_len=len(full_thinking),
            status="error" if stream_error else "ok",
            error=stream_error[:200] if stream_error else None,
        )

    if not full_response.strip():
        refusal_hint = ""
        if full_thinking:
            refusal_hint = f" (Thinking was present: {full_thinking[:100]}...)"
        raise ValueError(
            f"AI returned an empty response for '{debug_label}'.{refusal_hint}"
            f" See {scope_file} for details."
        )

    return parse_json_response(full_response)
