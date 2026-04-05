import re


def normalize_hm(text: str) -> str:
    """Normalize every case-insensitive variant of H&M to the canonical 'H&M'.

    Catches: h&m, H&M, h & m, H & M, h and m, H AND M, etc.
    """
    return re.sub(r"\b(?i)h\s*&\s*m\b|\bh\s+and\s+m\b", "H&M", text)
