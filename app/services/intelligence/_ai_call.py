"""Shared synchronous AI streaming helper for all intelligence stages."""

import json
import logging
from datetime import UTC, datetime

import httpx

from app.config import DATA_DIR
from app.core.async_utils import run_async
from app.services.ai_config import get_effective_config
from app.services.ai_provider import ai_provider
from app.services.intelligence._json import parse_json_response

logger = logging.getLogger(__name__)


def call_json_ai(
    *,
    system_prompt: str,
    user_prompt: str,
    options: dict,
    debug_label: str,
    model: str | None = None,
    db=None,
) -> dict:
    """Synchronous streaming AI call that returns a parsed JSON dict.

    Args:
        system_prompt: The system prompt to send.
        user_prompt: The user/document prompt to send.
        options: Provider options (num_ctx, temperature, num_predict, max_tokens, …).
        debug_label: Short label used in the debug log filename, e.g. "doc_42_entities".
        model: Override the configured summary model. If None, uses cfg.summary_model.
        db: SQLAlchemy session — if provided, reloads ai_provider config from DB first.

    Returns:
        Parsed dict from the AI JSON response.

    Raises:
        ValueError: If the AI returns an empty response, or if JSON parsing fails.
        httpx.HTTPStatusError: On non-2xx HTTP responses.
    """
    if db is not None:
        ai_provider.reload_from_db(db)

    cfg = get_effective_config(db)
    resolved_model = model or cfg.summary_model

    params = run_async(
        ai_provider.get_generate_params(
            model=resolved_model,
            prompt=user_prompt,
            system_prompt=system_prompt,
            stream=True,
            options=options,
        )
    )
    ptype = run_async(ai_provider.get_type())

    debug_dir = DATA_DIR / "ai_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = int(datetime.now(UTC).timestamp())
    debug_file = debug_dir / f"{debug_label}_{ts}.log"

    full_thinking = ""
    full_response = ""

    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        with open(debug_file, "a") as f:
            f.write(
                f"--- START: {debug_label} provider={ptype} model={resolved_model} ---\n"
            )
            f.write(f"Payload: {json.dumps(params['json'])}\n\n")

        try:
            with client.stream(
                "POST", params["url"], json=params["json"], headers=params["headers"]
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    chunk = ai_provider.parse_stream_line(line, ptype)
                    if not chunk:
                        continue

                    # Log tokens on-the-fly for partial-failure debugging
                    token = chunk.get("thinking", "") + chunk.get("response", "")
                    if token:
                        with open(debug_file, "a") as f:
                            f.write(token)

                    if "thinking" in chunk:
                        full_thinking += chunk["thinking"]
                    if "response" in chunk:
                        full_response += chunk["response"]
                    if chunk.get("done"):
                        break
        except Exception as e:
            with open(debug_file, "a") as f:
                f.write(f"\n--- ERROR DURING STREAM: {e} ---\n")
            raise

    with open(debug_file, "a") as f:
        f.write(
            f"\n--- END. response_len={len(full_response)} thinking_len={len(full_thinking)} ---\n"
        )

    if not full_response.strip():
        refusal_hint = ""
        if full_thinking:
            refusal_hint = f" (Thinking was present: {full_thinking[:100]}...)"
        raise ValueError(
            f"AI returned an empty response for '{debug_label}'.{refusal_hint}"
            f" See {debug_file} for details."
        )

    return parse_json_response(full_response)
