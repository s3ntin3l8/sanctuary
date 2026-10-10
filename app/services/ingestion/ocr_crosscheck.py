"""Second-opinion OCR: catch pages where chandra wrote text the scan does not show.

Chandra reads faint scans fluently but wrongly ("Sonnenstrasse" for
"Schloßgasse", "Sicherung" for "Schonung") and reports nothing unusual. A
classic detector (rapidocr) fails differently — it garbles faint text instead of
inventing words — so words chandra produced that the second reading has no
trace of are the ones to doubt.
"""

import difflib
import logging
import re
import threading

logger = logging.getLogger(__name__)

# A page is unverified when at least this share of chandra's words, and at
# least this many words, have no counterpart in the second reading.
UNVERIFIED_RATIO = 0.05
UNVERIFIED_MIN_TOKENS = 4
_MAX_STORED_TOKENS = 20
_FUZZY_CUTOFF = 0.7

# Chandra describes stamps and logos in English, sometimes after the markup on
# the same line; none of that is on the page, so the whole line is ignored.
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)[^\n]*")
_RE_TOKEN = re.compile(r"[a-zäöüß0-9]{4,}")
_RE_SQUASH = re.compile(r"[^a-zäöüß0-9]")

_ocr_instance = None
_ocr_lock = threading.Lock()
# One inference at a time: a second reading costs seconds against chandra's
# minutes, and rapidocr's thread-safety is not documented.
_run_lock = threading.Lock()


def get_ocr():
    """The shared rapidocr engine (also used by the scan slicer)."""
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_lock:
            if _ocr_instance is None:
                from rapidocr import RapidOCR

                _ocr_instance = RapidOCR()
    return _ocr_instance


def read_text(image) -> str:
    """Run rapidocr on a PIL image; raises when the engine fails."""
    import numpy as np

    arr = np.array(image.convert("RGB"))
    with _run_lock:
        # rapidocr 3.x returns an output object; ``txts`` is None on a blank page.
        out = get_ocr()(arr)
    return " ".join(t for t in out.txts or () if t and t.strip())


def second_opinion(png_bytes: bytes) -> str | None:
    """Second reading of one page image; ``None`` when it could not be made."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            return read_text(img)
    except Exception as exc:  # noqa: BLE001 — a failed cross-check never fails extraction
        logger.warning("second-opinion OCR failed: %s", exc)
        return None


def _tokens(text: str) -> list[str]:
    return _RE_TOKEN.findall(text.lower())


def check_page(chandra_markdown: str, second_text: str) -> dict:
    """Words chandra wrote that the second reading cannot account for.

    A word counts as supported when it appears in the second reading verbatim,
    inside it once spaces and punctuation are squashed away (a split or merged
    word), or as a close spelling of one of its words (the detector garbles).
    Returns ``{"unsupported": [...], "ratio": share, "count": n}``.
    """
    second = _tokens(second_text)
    known = set(second)
    squashed = _RE_SQUASH.sub("", second_text.lower())
    words = _tokens(_RE_IMAGE.sub(" ", chandra_markdown))
    unsupported: list[str] = []
    for word in dict.fromkeys(words):
        if word in known or word in squashed:
            continue
        if difflib.get_close_matches(word, known, n=1, cutoff=_FUZZY_CUTOFF):
            continue
        unsupported.append(word)
    return {
        "unsupported": unsupported[:_MAX_STORED_TOKENS],
        "count": len(unsupported),
        "ratio": round(len(unsupported) / max(1, len(set(words))), 3),
    }


def is_unverified(result: dict) -> bool:
    return (
        result["count"] >= UNVERIFIED_MIN_TOKENS and result["ratio"] >= UNVERIFIED_RATIO
    )
