"""Shared synchronous AI streaming helper for all intelligence stages."""

import fcntl
import json
import logging
import re
import time
import traceback
from datetime import UTC, datetime
from typing import TypeVar, overload

import httpx
from pydantic import BaseModel, ValidationError

from app.config import AI_READ_TIMEOUT, DATA_DIR
from app.core.async_utils import run_async
from app.services.ai_config import get_chat_config
from app.services.ai_provider import chat_provider
from app.services.intelligence._json import parse_json_response

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Thinking-loop watchdog: pathological-case safety net only. The primary
# anti-loop mechanism is the Qwen sampling config in ai_options.py
# (presence_penalty=1.5). This watchdog catches cases that escape that —
# it only triggers after ~4 minutes of pure thinking with zero response.
_THINK_WATCHDOG_CHARS = 16000
_THINK_WATCHDOG_SECS = 240.0

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


def _stream_response(
    *,
    params: dict,
    ptype,
    debug_label: str,
    resolved_model: str,
    ingest_batch_id: int | None,
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
                    # Abort when thinking consumes budget with zero response tokens
                    if (
                        not full_response
                        and len(full_thinking) > _THINK_WATCHDOG_CHARS
                        and (time.perf_counter() - start_perf) > _THINK_WATCHDOG_SECS
                    ):
                        logger.warning(
                            "call %s: thinking-loop watchdog triggered "
                            "(%d thinking chars, %.0fs elapsed) — aborting stream",
                            debug_label,
                            len(full_thinking),
                            time.perf_counter() - start_perf,
                        )
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
        # Provider-level thinking control. Qwen3.5 honours
        # chat_template_kwargs.enable_thinking=False on OpenAI-compat servers,
        # and Ollama's native top-level "think": false. The legacy /no_think
        # prefix was a Qwen3 trick — Qwen3.5 ignores it. ai_provider.py
        # translates this meta-flag into the right per-provider field.
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
    if db is not None:
        chat_provider.reload_from_db(db)

    cfg = get_chat_config(db)
    resolved_model = model or cfg.summary_model
    ptype = run_async(chat_provider.get_type())

    debug_dir = DATA_DIR / "ai_debug"
    scope_file = _scope_file(debug_dir, debug_label)

    # Pass 1: free-form analysis (only when two_pass=True).
    analysis = ""
    if two_pass:
        # Stage system prompts say "Return ONLY valid JSON". In pass 1 we
        # don't want JSON — we want the model to spend its budget on
        # reasoning, not on emitting structure that pass 2 will produce
        # under grammar enforcement. Override at the user-prompt level
        # (most recent instruction wins). Without this override, pass 1
        # routinely hits the stage's max_tokens cap mid-JSON-emit, leaving
        # pass 2 with truncated analysis context — observed empirically
        # (claims-p1 at 119-127% of cap, entities-p1 truncating mid-string).
        pass1_user_prompt = (
            f"{user_prompt}\n\n"
            f"--- Analysis pass: think through this carefully in plain "
            f"English. Do NOT output JSON yet — the structured JSON output "
            f"is produced in a follow-up step. Just analyze. ---"
        )
        pass1_label = f"{debug_label}-p1"
        pass1_params = _build_params(
            system_prompt=system_prompt,
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
    pass2_user_prompt = (
        (
            f"{user_prompt}\n\n"
            f"--- Your prior analysis ---\n{analysis.strip()}\n\n"
            f"--- Now output ONLY the JSON matching the schema. No prose. ---"
        )
        if (two_pass and analysis.strip())
        else user_prompt
    )
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
        # Single-pass: retry once with suppress_thinking=True if we haven't yet.
        # Two-pass: pass 2 already runs with suppress_thinking, so the same
        # escalation isn't available. Re-run the whole two-pass once for
        # sampling-noise recovery (sometimes a redo just works).
        should_retry = not suppress_thinking if two_pass else not pass2_suppress
        if should_retry:
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
