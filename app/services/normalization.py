import re

_HM_PATTERN = re.compile(r"\bh\s*&\s*m\b|\bh\s+and\s+m\b", re.IGNORECASE)


def normalize_hm(text: str) -> str:
    """Normalize every case-insensitive variant of H&M to the canonical 'H&M'.

    Catches: h&m, H&M, h & m, H & M, h and m, H AND M, etc.
    """
    return _HM_PATTERN.sub("H&M", text)
