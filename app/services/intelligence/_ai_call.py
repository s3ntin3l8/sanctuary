"""Shared synchronous AI streaming helper for all intelligence stages."""

import fcntl
import json
import logging
import re
import time
import traceback
from datetime import datetime
from typing import TypeVar, overload

import httpx
from pydantic import BaseModel, ValidationError

from app.config import AI_READ_TIMEOUT, DATA_DIR
from app.core.async_utils import run_async
from app.services.ai_config import get_chat_config
from app.services.ai_inflight import track_ai_call
from app.services.ai_provider import chat_provider
from app.services.intelligence._json import parse_json_response
from app.services.intelligence.prompts import (
    PASS1_USER_SUFFIX,
    PASS2_USER_SUFFIX,
    PROMPT_VERSION,
)
from app.services.timezone_service import get_user_tz

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Thinking-loop watchdog: pathological-case safety net only. The primary
# primary anti-loop mechanism is the Qwen sampling config in ai_options.py
# (presence_penalty=1.5). This watchdog catches cases that escape that —
# it only triggers after ~4 minutes of pure thinking with zero response.
_THINK_WATCHDOG_CHARS = 16000
_THINK_WATCHDOG_SECS = 240.0

# Watchdog for runaway prose in the main response channel. Qwen3.5 sometimes
# monologues for 30k+ characters in the response channel instead of thinking.
_RESPONSE_WATCHDOG_CHARS = 25000

# Stop sequences that match the literal self-correction phrases observed in loops.
_LOOP_STOP_SEQS = [
    "\nWait, actually",
    "\nWait, one more",
    "\n(Wait,",
    "\n(Okay, ready)",
]


# Pass-1 max-tokens override. None = use whatever the stage's options dict
# already specified (typically 6000-10000 per ai_options.STAGE_OPTIONS). The
# previous default of 1500 truncated every pass-1 mid-thought because
# Qwen3.5's reasoning chain alone runs ~2000 tokens before it gets to the
# answer. Callers who want a tighter cap can pass it explicitly.
_DEFAULT_PASS1_MAX_TOKENS: int | None = None

_SEPARATOR = "═" * 64
_SECTION = "──"
_LABEL_RE = re.compile(r"^(doc|batch|case)_(.+)_([^_]+)$")


def _parse_litellm_error_code(body: bytes) -> str | None:
    """Extract the error code from a LiteLLM JSON error response body."""
    try:
        err = json.loads(body).get("error") or {}
        return err.get("code") or err.get("type") or None
    except Exception:
        return None


def _parse_litellm_error_summary(body: bytes) -> str | None:
    """Extract a one-line human summary from a LiteLLM error response body.

    Returns a string like "MidStreamFallbackError: ... Model unloaded" suitable
    for splicing into our exception message and `runs.jsonl` error field. None
    when the body isn't a parseable litellm error envelope."""
    try:
        err = json.loads(body).get("error") or {}
    except Exception:
        return None
    msg = (err.get("message") or "").strip()
    typ = (err.get("type") or "").strip()
    if not msg and not typ:
        return None
    if typ and msg:
        return f"{typ}: {msg}"
    return msg or typ


# LM Studio behind the litellm proxy returns 4xx for several operational-warmup
# conditions that are actually transient: model loading, unloading mid-stream,
# or cold-start. The proxy normalizes the bodies to BadRequestError /
# MidStreamFallbackError; we look for the human-readable markers in the body
# summary captured by _parse_litellm_error_summary above.
_TRANSIENT_BACKEND_MARKERS = (
    "Failed to load model",
    "Model has not started loading",
    "Model unloaded",
    "MidStreamFallbackError",
    "Lm_studioException",
)


def is_transient_backend_error(exc: Exception) -> bool:
    """True when the exception message carries a marker indicating the backend
    model is in a transient operational state (loading/unloading/restart).

    Callers should treat these like 5xx — retry with backoff — rather than
    like genuine 4xx client errors which would warrant immediate-fail.
    """
    msg = str(exc)
    return any(m in msg for m in _TRANSIENT_BACKEND_MARKERS)


def _scope_file(debug_dir, debug_label: str, ingest_batch_id: int | None = None):
    """Derive the per-scope log file path from a debug_label."""
    m = _LABEL_RE.match(debug_label)
    if m:
        kind, scope_id, _ = m.groups()
        filename = f"{kind}_{scope_id}.md"
        if ingest_batch_id is not None:
            folder = debug_dir / f"ib-{ingest_batch_id:04d}"
            folder.mkdir(parents=True, exist_ok=True)
            return folder / filename
        elif kind == "case":
            return debug_dir / filename
        else:
            folder = debug_dir / "unbatched"
            folder.mkdir(parents=True, exist_ok=True)
            return folder / filename

    filename = f"misc_{debug_label}.md"
    folder = debug_dir / "unbatched"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename


def _shift_headers(text: str, depth: int = 4) -> str:
    """Prepend # depth times to any lines starting with # to subordinate them."""
    prefix = "#" * depth
    return re.sub(r"^(#+)", prefix + r"\1", text, flags=re.MULTILINE)


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
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    redact: bool = False,
) -> None:
    status = "error" if error else "ok"
    doc_id = int(scope_id) if kind == "doc" else None
    case_id = scope_id if kind == "case" else None

    header = f"{_SEPARATOR}\n"
    header += f"# call: {debug_label} | stage={stage} | ts={started_at} | prompt_v={PROMPT_VERSION}\n\n"

    ttfb_str = f"{ttfb_ms}" if ttfb_ms is not None else "n/a"
    meta = [
        f"model={model}",
        f"provider={provider}",
        f"duration={duration_ms}ms",
        f"ttfb={ttfb_str}ms",
        f"status={status}",
    ]

    # Add token usage metrics if available
    if prompt_tokens is not None:
        meta.append(f"prompt_tokens={prompt_tokens}")
    if completion_tokens is not None:
        meta.append(f"completion_tokens={completion_tokens}")
    if reasoning_tokens is not None:
        meta.append(f"reasoning_tokens={reasoning_tokens}")

    if doc_id:
        meta.append(f"doc_id={doc_id}")
    if ingest_batch_id:
        meta.append(f"ib={ingest_batch_id}")
    if case_id:
        meta.append(f"case_id={case_id}")

    header += f"**Metadata:** {' | '.join(meta)}\n\n"

    body = "## Payload\n\n"
    payload_copy = payload.copy()
    if "messages" in payload_copy:
        for msg in payload_copy["messages"]:
            role = msg.get("role", "unknown").capitalize()
            content = msg.get("content", "")

            body += f"### {role}\n"

            if redact:
                orig_len = len(content)
                content = f"[REDACTED {orig_len} chars]"
            elif len(content) > 5000:
                orig_len = len(content)
                content = (
                    content[:2500]
                    + f"\n\n... [TRUNCATED {orig_len - 3500} chars] ...\n\n"
                    + content[-1000:]
                )

            # Shift headers to subordinate them to the role (###)
            # depth=3 ensures # becomes #### (child) and ## becomes ##### (per user request)
            content = _shift_headers(content, depth=3)
            body += f"{content}\n\n"

        del payload_copy["messages"]

    if payload_copy:
        body += "### Other Parameters\n```json\n"
        body += json.dumps(payload_copy, indent=2, ensure_ascii=False)
        body += "\n```\n\n"

    if thinking:
        body += "## Thinking\n"
        body += f"{thinking}\n\n"

    if error:
        body += "## Error\n"
        body += f"{error}\n\n"
    else:
        # Wrap response in code block for structure
        body += "## Response\n"
        if "```" not in response:
            body += f"```json\n{response}\n```\n\n"
        else:
            body += f"{response}\n\n"

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
    doc_case_id: str | None,
    model: str,
    provider: str,
    duration_ms: int,
    ttfb_ms: int | None,
    response_len: int,
    thinking_len: int,
    status: str,
    error: str | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    watchdog: str | None = None,
) -> None:
    # Each entry carries three IDs so jq filters / log greps work uniformly
    # regardless of scope kind:
    #   doc_id    = the document this call relates to (when applicable)
    #   batch_id  = the ingest batch this call relates to (the call's primary
    #               scope when kind=batch, OR the doc's ingest_batch_id when
    #               kind=doc — same logical "batch this run belongs to")
    #   case_id   = the case this call relates to (call's scope when kind=case,
    #               OR the doc's case_id when kind=doc, when known)
    # Previously only the call's primary scope was reported, so doc-scoped
    # entries had batch_id=null + case_id=null even when the doc clearly
    # belonged to a batch and a case.
    doc_id = int(scope_id) if kind == "doc" else None
    if kind == "batch":
        batch_id: int | None = int(scope_id)
    elif kind == "doc" and ingest_batch_id is not None:
        batch_id = int(ingest_batch_id)
    else:
        batch_id = None
    if kind == "case":
        case_id: str | None = scope_id
    else:
        case_id = doc_case_id

    entry = {
        "ts": started_at,
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
        # Watchdog signal: None when the stream completed normally, "think_drain"
        # when the reasoning channel exceeded _THINK_WATCHDOG_CHARS within the
        # time budget, "response_monologue" when the response channel ran away.
        # The call may still report status=ok if the drained thinking channel
        # held a usable schema-constrained answer (channel promotion).
        "watchdog": watchdog,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    with open(index_file, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _stream_response(
    *,
    params: dict,
    ptype,
    debug_label: str,
    resolved_model: str,
    ingest_batch_id: int | None,
    doc_case_id: str | None = None,
    redact: bool = False,
) -> tuple[str, str]:
    """Stream one AI request, write its debug-log block, return (response, thinking).

    Owns: HTTP streaming, the thinking-loop watchdog, exception classification,
    debug-log write, runs.jsonl append. Does NOT parse JSON, validate schemas,
    promote channels, or retry — those decisions belong to `call_json_ai`.

    Raises on connection / timeout / unexpected errors. Empty response is
    returned, not raised — the caller decides whether empty is acceptable.
    """
    debug_dir = DATA_DIR / "ai_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    scope_file = _scope_file(debug_dir, debug_label, ingest_batch_id)
    index_file = debug_dir / "runs.jsonl"

    m = _LABEL_RE.match(debug_label)
    if m:
        kind, scope_id, stage = m.groups()
    else:
        kind, scope_id, stage = "misc", debug_label, debug_label

    started_at = datetime.now(get_user_tz()).isoformat(timespec="seconds")
    start_perf = time.perf_counter()
    ttfb_perf: float | None = None

    full_thinking = ""
    full_response = ""
    final_usage: dict | None = None
    stream_error: str | None = None
    _drain_mode = False
    _drain_deadline: float | None = None
    # When the watchdog fires, this records WHICH watchdog ("think_drain" or
    # "response_monologue"). Surfaced in runs.jsonl so log scans can find
    # silently-degraded calls (status=ok but answer came from the drained
    # thinking channel via promotion).
    watchdog_event: str | None = None

    try:
        with track_ai_call(debug_label):
            with httpx.Client(
                timeout=httpx.Timeout(
                    connect=5.0, read=AI_READ_TIMEOUT, write=30.0, pool=10.0
                )
            ) as client:
                with client.stream(
                    "POST",
                    params["url"],
                    json=params["json"],
                    headers=params["headers"],
                ) as response:
                    if not response.is_success:
                        _body = response.read()
                        _code = _parse_litellm_error_code(_body)
                        _summary = _parse_litellm_error_summary(_body)
                        if _code == "context_length_exceeded":
                            logger.warning(
                                "call %s: context length exceeded (HTTP %s) — "
                                "prompt too large for model context window",
                                debug_label,
                                response.status_code,
                            )
                        # Splice body summary into the exception text so it
                        # propagates into runs.jsonl (truncated to 200 chars
                        # at _append_index) and the per-scope debug log. Lets
                        # us diagnose Lm_studioException / MidStreamFallback /
                        # context_length_exceeded etc. without re-pulling the
                        # litellm /spend/logs/v2 endpoint after the fact.
                        raise httpx.HTTPStatusError(
                            f"HTTP {response.status_code}"
                            + (f" [{_code}]" if _code else "")
                            + (f" {_summary[:200]}" if _summary else ""),
                            request=response.request,
                            response=response,
                        )
                    for line in response.iter_lines():
                        if not line:
                            continue
                        chunk = chat_provider.parse_stream_line(line, ptype)
                        if not chunk:
                            continue

                        if chunk.get("usage"):
                            final_usage = chunk["usage"]

                        if chunk.get("done"):
                            break

                        # After a watchdog fires we keep reading to reach the final
                        # usage chunk (OpenAI sends it right before [DONE]) but
                        # discard all content. Give up after 30 s if it never arrives.
                        if _drain_mode:
                            if (
                                _drain_deadline is not None
                                and time.perf_counter() > _drain_deadline
                            ):
                                logger.warning(
                                    "call %s: drain timeout — giving up on usage chunk",
                                    debug_label,
                                )
                                break
                            continue

                        if "thinking" in chunk:
                            full_thinking += chunk["thinking"]
                        if "response" in chunk:
                            token = chunk["response"]
                            if token:
                                if ttfb_perf is None:
                                    ttfb_perf = time.perf_counter()
                                full_response += token

                        # Abort content accumulation when thinking consumes budget
                        # with zero response tokens; keep draining for usage chunk.
                        if (
                            not full_response
                            and len(full_thinking) > _THINK_WATCHDOG_CHARS
                            and (time.perf_counter() - start_perf)
                            > _THINK_WATCHDOG_SECS
                        ):
                            logger.warning(
                                "call %s: thinking-loop watchdog triggered "
                                "(%d thinking chars, %.0fs elapsed) — draining for usage",
                                debug_label,
                                len(full_thinking),
                                time.perf_counter() - start_perf,
                            )
                            _drain_mode = True
                            _drain_deadline = time.perf_counter() + 30.0
                            watchdog_event = "think_drain"

                        # Abort content accumulation when response monologue consumes budget
                        elif (
                            len(full_response) > _RESPONSE_WATCHDOG_CHARS
                            and (time.perf_counter() - start_perf)
                            > _THINK_WATCHDOG_SECS
                        ):
                            logger.warning(
                                "call %s: response-monologue watchdog triggered "
                                "(%d response chars, %.0fs elapsed) — draining for usage",
                                debug_label,
                                len(full_response),
                                time.perf_counter() - start_perf,
                            )
                            _drain_mode = True
                            _drain_deadline = time.perf_counter() + 30.0
                            watchdog_event = "response_monologue"
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
        if isinstance(e, httpx.HTTPStatusError):
            # Compact form: full traceback is in the scope log; the 200-char
            # index entry should carry the status + LiteLLM error code.
            stream_error = str(e)
        else:
            stream_error = "".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )
        raise
    finally:
        duration_ms = int((time.perf_counter() - start_perf) * 1000)
        ttfb_ms = (
            int((ttfb_perf - start_perf) * 1000) if ttfb_perf is not None else None
        )

        prompt_tokens = None
        completion_tokens = None
        reasoning_tokens = None
        if final_usage:
            prompt_tokens = final_usage.get("prompt_tokens")
            completion_tokens = final_usage.get("completion_tokens")
            # OpenAI / LiteLLM specific detail field
            details = final_usage.get("completion_tokens_details") or {}
            reasoning_tokens = details.get("reasoning_tokens")

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
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            redact=redact,
        )
        _append_index(
            index_file,
            started_at=started_at,
            kind=kind,
            scope_id=scope_id,
            stage=stage,
            ingest_batch_id=ingest_batch_id,
            doc_case_id=doc_case_id,
            model=resolved_model,
            provider=ptype,
            duration_ms=duration_ms,
            ttfb_ms=ttfb_ms,
            response_len=len(full_response),
            thinking_len=len(full_thinking),
            status="error" if stream_error else "ok",
            error=stream_error[:200] if stream_error else None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            watchdog=watchdog_event,
        )

    return full_response, full_thinking


def _build_params(
    *,
    system_prompt: str,
    user_prompt: str,
    options: dict,
    schema: type[BaseModel] | None,
    suppress_thinking: bool,
    resolved_model: str,
    pass1_max_tokens_override: int | None = None,
) -> dict:
    """Resolve provider-specific request params for one streaming call."""
    effective_options: dict = {
        **(options or {}),
        "stop": _LOOP_STOP_SEQS,
    }
    if suppress_thinking:
        effective_options["_enable_thinking"] = False
    if schema is not None:
        effective_options["_response_schema"] = schema.model_json_schema()
        effective_options["_schema_name"] = schema.__name__
    if pass1_max_tokens_override is not None:
        # Pass 1 has no schema-grammar to terminate generation, so cap explicitly.
        # Use the lower of the caller's max_tokens and the pass-1 cap.
        existing = effective_options.get("max_tokens")
        if (
            not isinstance(existing, int)
            or existing <= 0
            or existing > pass1_max_tokens_override
        ):
            effective_options["max_tokens"] = pass1_max_tokens_override

    return run_async(
        chat_provider.get_generate_params(
            model=resolved_model,
            prompt=user_prompt,
            system_prompt=system_prompt,
            stream=True,
            options=effective_options,
        )
    )


@overload
def call_json_ai(
    *,
    system_prompt: str,
    user_prompt: str,
    options: dict,
    debug_label: str,
    schema: type[T],
    model: str | None = ...,
    db=...,
    ingest_batch_id: int | None = ...,
    suppress_thinking: bool = ...,
    two_pass: bool = ...,
    pass1_max_tokens: int | None = ...,
    case_id: str | None = ...,
) -> T: ...


@overload
def call_json_ai(
    *,
    system_prompt: str,
    user_prompt: str,
    options: dict,
    debug_label: str,
    schema: None = None,
    model: str | None = ...,
    db=...,
    ingest_batch_id: int | None = ...,
    suppress_thinking: bool = ...,
    two_pass: bool = ...,
    pass1_max_tokens: int | None = ...,
    case_id: str | None = ...,
) -> dict: ...


def call_json_ai(
    *,
    system_prompt: str,
    user_prompt: str,
    options: dict,
    debug_label: str,
    schema: type[BaseModel] | None = None,
    model: str | None = None,
    db=None,
    ingest_batch_id: int | None = None,
    suppress_thinking: bool = False,
    two_pass: bool = False,
    pass1_max_tokens: int | None = _DEFAULT_PASS1_MAX_TOKENS,
    case_id: str | None = None,
) -> BaseModel | dict:
    """Synchronous streaming AI call returning a Pydantic model or parsed dict.

    Args:
        system_prompt: The system prompt to send.
        user_prompt: The user/document prompt to send.
        options: Provider options (num_ctx, temperature, num_predict, max_tokens, …).
        debug_label: Short label used in the debug log filename, e.g. "doc_42_entities".
        schema: Pydantic model class. When provided, the JSON schema is sent
            server-side via `response_format` / `format` to constrain output,
            and the parsed response is validated against the model. Returns
            an instance of the model. When None, returns the raw parsed dict.
        model: Override the configured summary model. If None, uses cfg.summary_model.
        db: SQLAlchemy session — if provided, reloads ai_provider config from DB first.
        ingest_batch_id: IngestBatch.id for cross-stage traceability in the index.
        suppress_thinking: When True, request thinking-disabled generation.
            Effective on Ollama (`think: false`); no-op on LMStudio + Qwen3.5
            (the `chat_template_kwargs.enable_thinking` field is ignored).
        two_pass: When True, run a free-form analysis pass first (no schema),
            then a schema-constrained formatting pass that ingests the
            analysis. Recovers chain-of-thought visibility at the cost of
            ~5–10× per-stage latency. The schema grammar otherwise masks
            every non-JSON token from position zero, eliminating thinking.
        pass1_max_tokens: Optional override for pass-1 max-tokens.
            None (default) = inherit the stage's `options.max_tokens`
            (typically 6000-10000 per ai_options.STAGE_OPTIONS). Set to a
            positive integer to cap pass-1 specifically — useful if a
            stage's prompt provokes runaway thinking. Ignored when
            `two_pass=False`.

    Returns:
        Pydantic model instance when `schema` is provided, else parsed dict.

    Raises:
        ValueError: If the (final) AI call returns an empty response or its
            JSON fails to parse.
        pydantic.ValidationError: If `schema` is provided and the response
            doesn't match — caller may catch this and fall back.
        httpx.HTTPStatusError: On non-2xx HTTP responses.
    """
    # Always load provider config from DB — critical for Celery workers whose
    # service-layer callers don't carry a db session.  Without this reload the
    # singleton falls back to ENV defaults (Ollama localhost:11434) on every
    # first AI call after worker startup, silently routing to the wrong backend.
    from app.services.user_settings_service import get_ai_debug_redact

    if db is not None:
        chat_provider.reload_from_db(db)
        cfg = get_chat_config(db)
        redact = get_ai_debug_redact(db)
    else:
        from app.dependencies import get_db_session

        _cfg_db = get_db_session()
        try:
            chat_provider.reload_from_db(_cfg_db)
            cfg = get_chat_config(_cfg_db)
            redact = get_ai_debug_redact(_cfg_db)
        finally:
            _cfg_db.close()

    resolved_model = model or cfg.summary_model
    ptype = run_async(chat_provider.get_type())

    debug_dir = DATA_DIR / "ai_debug"
    scope_file = _scope_file(debug_dir, debug_label, ingest_batch_id)

    # Pass 1: free-form analysis (only when two_pass=True).
    analysis = ""
    if two_pass:
        pass1_user_prompt = f"{user_prompt}\n\n{PASS1_USER_SUFFIX}"
        pass1_system_prompt = system_prompt
        pass1_label = f"{debug_label}-p1"
        pass1_params = _build_params(
            system_prompt=pass1_system_prompt,
            user_prompt=pass1_user_prompt,
            options=options,
            schema=None,  # crucial: no grammar → thinking unblocked
            suppress_thinking=False,  # let the model think
            resolved_model=resolved_model,
            pass1_max_tokens_override=pass1_max_tokens,
        )
        try:
            p1_response, p1_thinking = _stream_response(
                params=pass1_params,
                ptype=ptype,
                debug_label=pass1_label,
                resolved_model=resolved_model,
                ingest_batch_id=ingest_batch_id,
                doc_case_id=case_id,
                redact=redact,
            )
        except Exception as e:
            # Pass 1 errored — pass 2 can still run cold (with no analysis).
            # Logging happened inside _stream_response.
            logger.warning(
                "call %s pass-1 failed (%s); proceeding to pass 2 with no "
                "analysis context",
                debug_label,
                e,
            )
            p1_response, p1_thinking = "", ""

        # Pass-1 promotion: capture reasoning even if it landed on the
        # thinking channel (LMStudio reasoning toggle on, Ollama default).
        # Unlike pass 2's schema-aware promotion, pass 1 takes any thinking
        # content as the analysis since there's no JSON shape to validate.
        if not p1_response.strip() and p1_thinking.strip():
            logger.info(
                "call %s pass-1: empty response — promoting thinking channel "
                "to analysis (%d chars)",
                debug_label,
                len(p1_thinking),
            )
            analysis = p1_thinking
        else:
            analysis = p1_response

    # Pass 2 (or single-pass): schema-constrained formatting.
    if two_pass and analysis.strip():
        pass2_user_prompt = (
            f"{user_prompt}\n\n"
            f"--- Your prior analysis ---\n{analysis.strip()}\n\n"
            f"{PASS2_USER_SUFFIX}"
        )
    else:
        pass2_user_prompt = f"{user_prompt}\n\n{PASS2_USER_SUFFIX}"
    # Pass 2 (or single-pass) suppress_thinking: in two-pass we already have
    # the model's reasoning from pass 1, so suppress thinking for the format
    # pass. In single-pass, honor the caller's flag.
    pass2_suppress = True if two_pass else suppress_thinking
    pass2_label = f"{debug_label}-p2" if two_pass else debug_label

    pass2_params = _build_params(
        system_prompt=system_prompt,
        user_prompt=pass2_user_prompt,
        options=options,
        schema=schema,
        suppress_thinking=pass2_suppress,
        resolved_model=resolved_model,
    )

    full_response, full_thinking = _stream_response(
        params=pass2_params,
        ptype=ptype,
        debug_label=pass2_label,
        resolved_model=resolved_model,
        ingest_batch_id=ingest_batch_id,
        doc_case_id=case_id,
        redact=redact,
    )

    # Qwen3.5 + LMStudio + structured output: the schema-constrained JSON
    # frequently arrives entirely through `reasoning_content` rather than
    # `content`, even with `enable_thinking=False` set. The grammar still
    # forced the right shape — only the channel is wrong. When we asked for
    # a schema and the response channel is empty but the reasoning channel
    # looks like the JSON answer, promote it.
    if (
        schema is not None
        and not full_response.strip()
        and full_thinking
        and "{" in full_thinking
        and "}" in full_thinking
    ):
        logger.info(
            "call %s: empty content but reasoning_content holds the schema-"
            "constrained answer — promoting reasoning to response",
            pass2_label,
        )
        full_response = full_thinking
        full_thinking = ""

    if not full_response.strip():
        # Watchdog-drain short-circuit: when pass-2's thinking channel passed
        # _THINK_WATCHDOG_CHARS, the model spun in a reasoning loop and the
        # stream was drained without ever producing a parsable answer.
        # Retrying the same prompt is denial, not error-handling — the next
        # attempt almost always reproduces the loop, multiplying wallclock
        # and token cost (240s × N). Raise ValueError directly; outer
        # callers like batch_analyzer.analyze() already have their own
        # fallback path (e.g. a single retry with suppress_thinking from
        # the service layer, batch_analyzer.py:632–659).
        watchdog_drained = len(full_thinking) > _THINK_WATCHDOG_CHARS
        # Single-pass: retry once with suppress_thinking=True if we haven't yet.
        # Two-pass: pass 2 already runs with suppress_thinking, so the same
        # escalation isn't available. Re-run the whole two-pass once for
        # sampling-noise recovery (sometimes a redo just works).
        should_retry = not suppress_thinking if two_pass else not pass2_suppress
        if should_retry and not watchdog_drained:
            logger.info(
                "call %s: empty response — retrying once with suppress_thinking",
                pass2_label,
            )
            return call_json_ai(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                options=options,
                debug_label=debug_label,
                schema=schema,
                model=model,
                db=db,
                ingest_batch_id=ingest_batch_id,
                suppress_thinking=True,
                two_pass=two_pass,
                pass1_max_tokens=pass1_max_tokens,
                case_id=case_id,
            )
        if watchdog_drained:
            logger.warning(
                "call %s: watchdog drained pass-2 (thinking=%d chars) — "
                "skipping inner retry to avoid AI-call multiplier; outer "
                "caller handles fallback",
                pass2_label,
                len(full_thinking),
            )
        refusal_hint = ""
        if full_thinking:
            refusal_hint = f" (Thinking was present: {full_thinking[:100]}...)"
        raise ValueError(
            f"AI returned an empty response for '{pass2_label}'.{refusal_hint}"
            f" See {scope_file} for details."
        )

    parsed = parse_json_response(full_response)
    if schema is None:
        return parsed
    try:
        return schema.model_validate(parsed)
    except ValidationError as e:
        # Server-enforced grammar should make this rare, but older Ollama
        # builds and non-compliant local models can still drift. Surface the
        # specific schema-violation cause so callers can catch and fall back.
        raise ValueError(
            f"AI response failed schema validation for '{pass2_label}' "
            f"({schema.__name__}): {e}. See {scope_file} for the raw response."
        ) from e
