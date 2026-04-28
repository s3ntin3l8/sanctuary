"""Locate AI-quoted text inside source markdown.

Used by the enrichment pipeline (stamp offsets at write time, see
``document_enricher._repair_passage_offsets``) and by the HUD renderer
(resolve claim-excerpt offsets at read time, see ``render_highlighted``).

Three passes:

1. exact substring (must be unique)
2. normalized substring — whitespace collapsed, curly quotes folded (must be unique)
3. fuzzy windowed difflib ratio ≥ 0.85 (only for text ≥ 20 chars)

Returns ``(start, end)`` byte offsets in the original ``content`` string, or
``None`` when the passage cannot be confidently located.
"""

from __future__ import annotations

import difflib
import re


def _normalize(s: str) -> str:
    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()


def _norm_to_orig(content: str, norm_idx: int) -> int:
    """Map a position in the normalized projection of ``content`` back to
    its offset in the original ``content`` string."""
    norm_walked = 0
    for m in re.finditer(r"\S+|\s+", content):
        token = _normalize(m.group())
        if norm_walked + len(token) > norm_idx:
            return m.start()
        norm_walked += len(token) + 1  # +1 for collapsed space
    return len(content)


def find_text_offsets(content: str, text: str) -> tuple[int, int] | None:
    """Locate ``text`` inside ``content``. Returns ``(start, end)`` or ``None``."""
    if not text or not content:
        return None

    # Pass 1 — exact, unique
    idx = content.find(text)
    if idx != -1 and content.find(text, idx + 1) == -1:
        return idx, idx + len(text)

    # Pass 2 — normalized, unique
    norm_text = _normalize(text)
    norm_content = _normalize(content)
    if norm_text:
        norm_idx = norm_content.find(norm_text)
        if norm_idx != -1 and norm_content.find(norm_text, norm_idx + 1) == -1:
            orig_idx = _norm_to_orig(content, norm_idx)
            return orig_idx, min(orig_idx + len(text), len(content))

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
            orig_idx = _norm_to_orig(content, best_pos)
            return orig_idx, min(orig_idx + len(text), len(content))

    return None
