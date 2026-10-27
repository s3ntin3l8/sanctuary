"""Locate AI-quoted text inside source markdown.

Used by the enrichment pipeline (stamp offsets at write time, see
``document_enricher._repair_passage_offsets``) and by the HUD renderer
(resolve claim-excerpt offsets at read time, see ``render_highlighted``).

Three passes:

1. exact substring (must be unique)
2. normalized substring — whitespace collapsed, curly quotes folded (must be unique)
3. fuzzy windowed difflib ratio ≥ 0.85 (only for text ≥ 20 chars)

Returns ``(start, end)`` character offsets in the original ``content`` string, or
``None`` when the passage cannot be confidently located.
"""

from __future__ import annotations

import difflib
from array import array

# A span is widened to word boundaries by at most this many characters; a longer
# alphanumeric run (hash, base64, id) is not a word, so the span stays as matched.
_SNAP_MAX = 32

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _project(s: str) -> tuple[str, array]:
    """Normalize ``s`` (quotes folded, whitespace runs collapsed, ends
    stripped) and return it with, for every normalized character, its offset
    in ``s``."""
    chars: list[str] = []
    origin = array("l")
    pending_space = False
    for i, ch in enumerate(s.translate(_QUOTES)):
        if ch.isspace():
            pending_space = bool(chars)
            continue
        if pending_space:
            chars.append(" ")
            origin.append(i - 1)
            pending_space = False
        chars.append(ch)
        origin.append(i)
    return "".join(chars), origin


def _snap_to_words(content: str, start: int, end: int) -> tuple[int, int]:
    """Widen a span so it does not begin or end inside a word (by at most
    ``_SNAP_MAX`` characters), then drop whitespace at its edges."""
    lo = start
    while (
        lo > 0
        and content[lo - 1].isalnum()
        and content[lo].isalnum()
        and start - lo < _SNAP_MAX
    ):
        lo -= 1
    if lo > 0 and content[lo - 1].isalnum() and content[lo].isalnum():
        lo = start
    hi = end
    while (
        hi < len(content)
        and content[hi - 1].isalnum()
        and content[hi].isalnum()
        and hi - end < _SNAP_MAX
    ):
        hi += 1
    if hi < len(content) and content[hi - 1].isalnum() and content[hi].isalnum():
        hi = end
    while lo < hi and content[lo].isspace():
        lo += 1
    while hi > lo and content[hi - 1].isspace():
        hi -= 1
    return lo, hi


def find_text_offsets(content: str, text: str) -> tuple[int, int] | None:
    """Locate ``text`` inside ``content``. Returns ``(start, end)`` or ``None``."""
    if not text or not content:
        return None

    # Pass 1 — exact, unique
    idx = content.find(text)
    if idx != -1 and content.find(text, idx + 1) == -1:
        return idx, idx + len(text)

    # Pass 2 — normalized, unique
    norm_text, _ = _project(text)
    norm_content, origin = _project(content)
    if norm_text:
        norm_idx = norm_content.find(norm_text)
        if norm_idx != -1 and norm_content.find(norm_text, norm_idx + 1) == -1:
            return _snap_to_words(
                content, origin[norm_idx], origin[norm_idx + len(norm_text) - 1] + 1
            )

    # Pass 3 — fuzzy windowed ratio (paraphrased / punctuation-shifted quotes)
    target_len = len(norm_text)
    if 20 <= target_len <= len(norm_content):
        step = max(8, target_len // 4)
        best_ratio = 0.0
        best_pos = -1
        for i in range(0, len(norm_content) - target_len + 1, step):
            window = norm_content[i : i + target_len]
            sm = difflib.SequenceMatcher(None, norm_text, window)
            if sm.real_quick_ratio() < 0.7 or sm.quick_ratio() < 0.75:
                continue
            ratio = sm.ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_pos = i
        if best_ratio >= 0.85 and best_pos >= 0:
            return _snap_to_words(
                content, origin[best_pos], origin[best_pos + target_len - 1] + 1
            )

    return None
