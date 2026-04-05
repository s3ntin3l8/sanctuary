import re


def normalize_hm(text: str) -> str:
    """Normalize every case-insensitive variant of H&M to the canonical 'H&M'.

    Catches: h&m, H&M, h & m, H & M, h and m, H AND M, etc.
    """
    return re.sub(r"(?i)h\s*&\s*m|h\s+and\s+m", "H&M", text)
