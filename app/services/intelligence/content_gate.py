"""Central gate: is a document's content usable for AI processing?"""

import re

_PLACEHOLDER_RE = re.compile(r"<!--[^>]*-->")
_MIN_WORD_CHARS = 30


def is_content_ai_ready(doc) -> bool:
    """Return True if the document has enough real text for AI stages.

    Rejects documents whose content is empty, a Docling failure message,
    or dominated by image/page-break placeholders with no actual words.
    """
    content = doc.content
    if not content:
        return False
    stripped = content.strip()
    if not stripped or stripped.startswith("Conversion failed:"):
        return False
    cleaned = _PLACEHOLDER_RE.sub("", stripped)
    word_chars = len(re.sub(r"\s+", "", cleaned))
    return word_chars >= _MIN_WORD_CHARS
