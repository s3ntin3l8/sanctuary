"""Text normalization helpers.

`normalize_hm`           — Jinja filter; canonicalizes H&M variants in display text.
`normalize_entity_name`  — canonical form for Entity dedup at extraction time.
"""

import re
import unicodedata

from app.models.enums import EntityType

_HM_PATTERN = re.compile(r"\bh\s*&\s*m\b|\bh\s+and\s+m\b", re.IGNORECASE)


def normalize_hm(text: str) -> str:
    """Canonicalize 'H&M' / 'h and m' / 'H & M' → 'H&M'."""
    return _HM_PATTERN.sub("H&M", text)


# Honorifics stripped from PERSON names only. Order-dependent — longer first.
_PERSON_HONORIFICS = (
    "prof. dr.",
    "rechtsanwältin",
    "rechtsanwalt",
    "prof.",
    "dr.",
    "herr",
    "frau",
    "ra ",
    "rain ",
)

# German transliteration fold applied AFTER diacritic strip so
# "Björn"/"Bjoern", "Müller"/"Mueller", "Schröder"/"Schroeder",
# "Köln"/"Koeln" all collapse to the same canonical form.
_GERMAN_FOLD = (
    ("oe", "o"),
    ("ue", "u"),
    ("ae", "a"),
)


def _strip_diacritics(s: str) -> str:
    """ö→o, ü→u, ä→a, ß→ss, é→e, etc."""
    s = s.replace("ß", "ss")
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _german_fold(s: str) -> str:
    """Fold ASCII-form German umlaut digraphs to single vowels."""
    for src, dst in _GERMAN_FOLD:
        s = s.replace(src, dst)
    return s


def _strip_person_honorifics(s: str) -> str:
    lower = s.lower().strip()
    for h in _PERSON_HONORIFICS:
        if lower.startswith(h):
            return s[len(h) :].strip()
    return s


def _comma_reverse(s: str) -> str:
    """'Liu, Yingying' → 'Yingying Liu' when the prefix is a single token."""
    if "," not in s:
        return s
    parts = [p.strip() for p in s.split(",", 1)]
    if len(parts) == 2 and parts[0] and " " not in parts[0]:
        return f"{parts[1]} {parts[0]}"
    return s


def _sub_unit_collapse(s: str, canonical_names: set[str]) -> str:
    """'Landratsamt X, Amt Y' → 'Landratsamt X' when normalized 'Landratsamt X'
    is present in the (already-normalized) canonical_names set.
    """
    if ", " not in s:
        return s
    prefix = s.split(", ", 1)[0]
    prefix_key = _german_fold(_strip_diacritics(prefix).lower())
    if prefix_key in canonical_names:
        return prefix
    return s


def normalize_entity_name(
    name: str,
    entity_type: EntityType,
    canonical_names: set[str] | None = None,
) -> str:
    """Return the canonical dedup key for an entity name.

    The original `name` should still be stored as-is on the Entity row; this
    function only produces the lookup key. Conservative — no Levenshtein or
    other fuzzy matching, only deterministic German-transliteration folds.
    """
    if not name or not name.strip():
        return ""

    s = name.strip()

    if entity_type == EntityType.PERSON:
        s = _strip_person_honorifics(s)
        s = _comma_reverse(s)
    elif entity_type in (
        EntityType.ORGANIZATION,
        EntityType.COURT,
        EntityType.LAW_FIRM,
    ):
        if canonical_names:
            s = _sub_unit_collapse(s, canonical_names)

    s = _strip_diacritics(s)
    s = s.lower()
    s = _german_fold(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
