"""Shared JSON-robustness utilities for AI response parsing."""

import json
import logging
import re

logger = logging.getLogger(__name__)


_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")


def is_effectively_empty(text: str | None) -> bool:
    """True for None/whitespace AND for an empty markdown fence like ```json\\n\\n```.

    qwen3.5 + LMStudio occasionally emits *just* an opening + closing fence as
    its response content, with the actual schema-conformant JSON sitting in
    the reasoning_content channel.  The empty fence is 12 non-empty chars, so
    a plain `not text.strip()` check treats it as a real response and the
    JSON-in-thinking promotion logic at `_ai_call.py` is bypassed.

    This helper narrowly recognizes that case: strip ONE leading and ONE
    trailing markdown fence delimiter, then check whether anything remains.
    A valid fenced payload like ```json\\n{"x": 1}\\n``` still has content
    after stripping the fences, so this returns False for it.
    """
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    s = _FENCE_OPEN_RE.sub("", s, count=1)
    s = _FENCE_CLOSE_RE.sub("", s, count=1)
    return not s.strip()


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

    start = raw_text.find("{")
    if start == -1:
        raise ValueError(f"AI response contains no JSON object: {raw_text[:100]}...")
    end = raw_text.rfind("}")

    if end > start:
        # Standard case: trim to the substring between the first `{` and the
        # last `}` — handles "text before {...} text after" wrappers.
        candidate = raw_text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # The {...} substring isn't valid JSON. Fall through to the
            # truncation repair, which counts unmatched openers in the
            # original tail and appends the right closers.
            pass

    raw_text = _close_truncated_json(raw_text[start:])

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        # Last-ditch repair: a complete-looking response (`{...}`) may still be
        # nested-truncated if the LLM wrapped a partial object in extra braces.
        repaired = _close_truncated_json(raw_text)
        if repaired != raw_text:
            try:
                return json.loads(repaired)
            except Exception:
                pass
        logger.debug(f"Malformed JSON from AI: {raw_text}")
        raise ValueError(
            f"Failed to parse AI response as JSON. Length: {len(raw_text)}. Preview: {raw_text[:100]}..."
        ) from e


def _close_truncated_json(text: str) -> str:
    """Append the closers needed to balance unmatched `{`, `[`, and `"` tokens.

    Walks the text and tracks unmatched openers, ignoring brackets inside
    string literals (handles escaped quotes). On truncation, emits the closers
    in the reverse order they were opened. AI responses that truncate mid-
    object or mid-string-value — common when `num_predict` is hit — round-trip
    through this and parse cleanly.

    If the walk ends inside a string literal (`in_string=True`), a closing `"`
    is prepended to the bracket closers so the resulting text is valid JSON.
    """
    stack: list[str] = []
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()

    # Close an open string literal before closing any bracket openers so the
    # result is always syntactically valid JSON.
    prefix = '"' if in_string else ""
    return text + prefix + "".join(reversed(stack))
