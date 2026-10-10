"""Prompt-side cleanup of OCR text.

Chandra writes a description for every stamp, logo and crest it sees —
``![Official stamp of the Amtsgericht … dated 28. AUG. 2026]()`` — and sometimes
an English narration of the same image on the rest of the line. None of it is on
the page as text. ``Document.content`` keeps it verbatim (passage offsets, pins
and the reader's caption chips point into it); these helpers are for the text
that is fed to models and embeddings.
"""

import re

# The markup plus the rest of its line, where Chandra puts the narration.
_RE_IMAGE_LINE = re.compile(r"!\[([^\]]*)\]\([^)]*\)[^\n]*")


def strip_image_lines(text: str) -> str:
    """Replace every image description line with a space."""
    return _RE_IMAGE_LINE.sub(" ", text)


def _condense(match: re.Match[str]) -> str:
    alt = " ".join(match.group(1).split())
    # A stamp's alt text carries the received date; a logo's does not.
    if any(ch.isdigit() for ch in alt):
        return f"[Bild: {alt}]"
    return ""


def condense_image_descriptions(text: str) -> str:
    """Keep dated stamp descriptions as ``[Bild: …]``; drop the rest, narration included."""
    if "![" not in text:
        return text
    return _RE_IMAGE_LINE.sub(_condense, text)
