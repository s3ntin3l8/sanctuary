"""Shared JSON-robustness utilities for AI response parsing."""

import json
import logging
import re

logger = logging.getLogger(__name__)


def parse_json_response(raw_text: str) -> dict:
    """Strip markdown fences and parse JSON from an AI response."""
    if not raw_text or not raw_text.strip():
        raise ValueError("AI returned an empty response")

    raw_text = raw_text.strip()

    if "```" in raw_text:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if match:
            raw_text = match.group(1).strip()
        else:
            parts = raw_text.split("```")
            if len(parts) >= 3:
                raw_text = parts[1].strip()
                if raw_text.lower().startswith("json"):
                    raw_text = raw_text[4:].strip()

    if not (raw_text.startswith("{") and raw_text.endswith("}")):
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start != -1 and end != -1:
            raw_text = raw_text[start : end + 1]
        elif start != -1:
            raw_text = raw_text[start:] + "}"
        else:
            raise ValueError(
                f"AI response contains no JSON object: {raw_text[:100]}..."
            )

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        try:
            return json.loads(raw_text.strip())
        except Exception:
            logger.debug(f"Malformed JSON from AI: {raw_text}")
            raise ValueError(
                f"Failed to parse AI response as JSON. Length: {len(raw_text)}. Preview: {raw_text[:100]}..."
            ) from e
